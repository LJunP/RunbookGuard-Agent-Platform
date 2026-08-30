"""动作工具的执行器。

三段分离的第三段，与 ReadOnlyToolExecutor 分开：动作工具的前置条件不同
（必须持有已消费的审批）、沙箱要求不同（M0 §9 要求独立进程/容器）、
幂等要求不同（必须携带 idempotency key）。合成一个类会让这些差异被 if 分支淹没。

**沙箱（M8 落地）**：动作在受限子进程里执行，见 action_sandbox.py 的逐条对照表。
进程级隔离覆盖了非 root / CPU / 内存 / PID / 强制取消 / 输出上限；
挂载隔离与只读根文件系统做不到（需要容器），egress 阻断由 K8s NetworkPolicy 提供。

`sandbox=False` 保留同进程路径，仅供单元测试使用——它跑在没有 fork 开销的
环境里，且不需要验证隔离本身。任何生产路径都必须走 sandbox=True。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from ..tools.catalogue import BY_NAME
from ..tools.contract import ToolAuthorization, ToolContract
from .executor import ToolFailure, ToolResult


@dataclass(frozen=True)
class ConsumedApproval:
    """已在 Java 侧完成四项比对并消费的审批。

    Executor 要求它作为参数，因此「没消费审批就执行动作」在签名上就不成立。
    这个类型只能由 ApprovalGateway.consume 成功后构造。
    """

    approval_id: str
    tool_name: str
    resource_ref: str
    arguments_digest: str
    _issuer: str = ""

    def __post_init__(self) -> None:
        if self._issuer != _CONSUMER_TOKEN:
            raise PermissionError(
                "ConsumedApproval may only be created after a successful consume() call"
            )


_CONSUMER_TOKEN = "approval-gateway-consume"


def mark_consumed(
    *, approval_id: str, tool_name: str, resource_ref: str, arguments_digest: str
) -> ConsumedApproval:
    """ApprovalGateway 专用。grep 这个函数名即可穷举所有「审批已消费」的断言点。"""
    return ConsumedApproval(
        approval_id=approval_id,
        tool_name=tool_name,
        resource_ref=resource_ref,
        arguments_digest=arguments_digest,
        _issuer=_CONSUMER_TOKEN,
    )


class ActionToolExecutor:
    """动作执行器。

    sandbox=True（默认）走独立子进程；False 走同进程 HTTP，仅供单元测试。
    默认值刻意是 True：一个「默认不隔离、需要显式打开」的沙箱等于没有沙箱，
    因为漏掉一处就是一个可执行的缺口。
    """

    def __init__(
        self,
        lab_base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        sandbox: bool = True,
    ) -> None:
        self.lab_base_url = lab_base_url.rstrip("/")
        self._client = client
        self._sandbox = sandbox
        # egress allowlist：子进程只被允许连这一个地址。
        # 单元素集合看起来多余，但它让「允许连哪里」成为一个显式的、
        # 可以被审查与扩展的数据，而不是隐含在 base_url 这个变量里。
        self._allowed_base_urls = frozenset({self.lab_base_url})

    async def execute(
        self, authorization: ToolAuthorization, consumed: ConsumedApproval
    ) -> ToolResult:
        if not authorization.allowed:
            raise AssertionError(
                f"action executor received a denied authorization for {authorization.tool_name}"
            )
        contract = BY_NAME.get(authorization.tool_name)
        if contract is None or not contract.is_write():
            raise AssertionError(
                f"{authorization.tool_name} is not a write tool; use ReadOnlyToolExecutor"
            )

        # 三项交叉校验。Java 侧已经比过一次，这里再比一次是因为
        # consume 与 execute 之间仍有一个时间窗口，而这两个对象可能来自不同调用路径。
        if consumed.tool_name != contract.name:
            raise ToolFailure(
                "digest_mismatch",
                f"consumed approval is for {consumed.tool_name}, not {contract.name}",
                tool_name=contract.name,
            )
        if consumed.resource_ref != authorization.binding.resource_ref:
            raise ToolFailure(
                "digest_mismatch",
                f"consumed approval is for {consumed.resource_ref}, "
                f"not {authorization.binding.resource_ref}",
                tool_name=contract.name,
            )
        if consumed.arguments_digest != authorization.arguments_digest:
            raise ToolFailure(
                "digest_mismatch",
                "authorization digest does not match the consumed approval",
                tool_name=contract.name,
            )

        idempotency_key = _render_key(contract, authorization, consumed)

        if self._sandbox:
            payload = await self._execute_sandboxed(contract, authorization, idempotency_key)
            return ToolResult(
                tool_name=contract.name,
                payload=payload,
                result_bytes=len(str(payload)),
                untrusted=False,
            )

        handler = _ACTION_HANDLERS.get(contract.name)
        if handler is None:
            raise ToolFailure(
                "invalid_arguments", "no handler registered", tool_name=contract.name
            )

        try:
            payload = await asyncio.wait_for(
                handler(self, contract, authorization.arguments, idempotency_key),
                timeout=contract.timeout_seconds,
            )
        except TimeoutError as exc:
            # 动作超时是最危险的失败：可能已生效也可能没有。
            # 因此**不重试**（契约里 max_attempts=1），交由人工从动作账本核实。
            raise ToolFailure(
                "tool_timeout",
                f"exceeded {contract.timeout_seconds}s; effect is UNKNOWN, check the action ledger",
                tool_name=contract.name,
            ) from exc
        return ToolResult(
            tool_name=contract.name,
            payload=payload,
            result_bytes=len(str(payload)),
            # 动作结果来自受控代码而非外部数据源，但仍不作为指令解析。
            untrusted=False,
        )

    async def _execute_sandboxed(
        self,
        contract: ToolContract,
        authorization: ToolAuthorization,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """在受限子进程里执行，并把结果整形成契约声明的输出。

        SandboxError 的 failure_code 直接透传给 ToolFailure：它已经是契约里的
        typed failure 取值，在这里重新分类只会丢信息。
        """
        from .action_sandbox import SandboxError, run_action_in_subprocess

        try:
            result = await run_action_in_subprocess(
                tool_name=contract.name,
                arguments=dict(authorization.arguments),
                idempotency_key=idempotency_key,
                lab_base_url=self.lab_base_url,
                allowed_base_urls=self._allowed_base_urls,
                timeout_seconds=contract.timeout_seconds,
            )
        except SandboxError as exc:
            raise ToolFailure(
                exc.failure_code, str(exc), tool_name=contract.name
            ) from exc

        shaper = _RESULT_SHAPERS[contract.name]
        return shaper(result.payload, dict(authorization.arguments), idempotency_key)

    async def _post(
        self, contract: ToolContract, path: str, body: dict[str, Any]
    ) -> dict:
        client = self._client or httpx.AsyncClient(
            timeout=max(1.0, contract.timeout_seconds - 2.0)
        )
        owns = self._client is None
        try:
            response = await client.post(f"{self.lab_base_url}{path}", json=body)
        except httpx.TimeoutException as exc:
            raise ToolFailure("tool_timeout", str(exc), tool_name=contract.name) from exc
        except httpx.HTTPError as exc:
            raise ToolFailure("upstream_unavailable", str(exc), tool_name=contract.name) from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code >= 400:
            body_json = _safe_json(response)
            code = body_json.get("error", "")
            if code in {"target_version_already_deployed", "rate_out_of_range"}:
                raise ToolFailure("invalid_arguments", body_json.get("message", code),
                                  tool_name=contract.name)
            raise ToolFailure(
                "sandbox_failure",
                f"synthetic-lab returned {response.status_code}: {body_json}",
                tool_name=contract.name,
            )
        return _safe_json(response)


def _safe_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _render_key(
    contract: ToolContract, authorization: ToolAuthorization, consumed: ConsumedApproval
) -> str:
    """幂等键从模板渲染。

    键里含 approval_id：同一份审批只能产生一次副作用，而不同审批（即使参数相同）
    是不同的意图。这与 ADR-0003 「幂等键表达业务意图」一致。
    """
    template = contract.idempotency_key_template or ""
    return template.format(
        tenant_id=authorization.binding.tenant_id,
        service=authorization.arguments.get("service", "unknown"),
        approval_id=consumed.approval_id,
    )


async def _restart(
    executor: ActionToolExecutor, contract: ToolContract, args: dict, key: str
) -> dict:
    body = await executor._post(
        contract, "/v1/actions/restart", {"idempotency_key": key, "service": args["service"]}
    )
    return {
        "service": body.get("service", args["service"]),
        "restarted": True,
        "idempotency_key": key,
        "replayed": body.get("replayed", False),
    }


async def _rollback(
    executor: ActionToolExecutor, contract: ToolContract, args: dict, key: str
) -> dict:
    body = await executor._post(
        contract,
        "/v1/actions/rollback",
        {
            "idempotency_key": key,
            "service": args["service"],
            "target_version": args["target_version"],
        },
    )
    return {
        "service": body.get("service", args["service"]),
        "rolled_back_to": args["target_version"],
        "idempotency_key": key,
        "replayed": body.get("replayed", False),
    }


async def _throttle(
    executor: ActionToolExecutor, contract: ToolContract, args: dict, key: str
) -> dict:
    body = await executor._post(
        contract,
        "/v1/actions/throttle",
        {
            "idempotency_key": key,
            "service": args["service"],
            "rate_basis_points": args["rate_basis_points"],
        },
    )
    return {
        "service": body.get("service", args["service"]),
        "rate_basis_points": args["rate_basis_points"],
        "idempotency_key": key,
        "replayed": body.get("replayed", False),
    }


_ACTION_HANDLERS = {
    "restart_synthetic_service": _restart,
    "rollback_synthetic_deployment": _rollback,
    "throttle_synthetic_traffic": _throttle,
}


def _shape_restart(lab: dict, args: dict, key: str) -> dict:
    return {
        "service": lab.get("service", args["service"]),
        "restarted": True,
        "idempotency_key": key,
        "replayed": bool(lab.get("replayed", False)),
    }


def _shape_rollback(lab: dict, args: dict, key: str) -> dict:
    return {
        "service": lab.get("service", args["service"]),
        "rolled_back_to": args["target_version"],
        "idempotency_key": key,
        "replayed": bool(lab.get("replayed", False)),
    }


def _shape_throttle(lab: dict, args: dict, key: str) -> dict:
    return {
        "service": lab.get("service", args["service"]),
        "rate_basis_points": args["rate_basis_points"],
        "idempotency_key": key,
        "replayed": bool(lab.get("replayed", False)),
    }


# 沙箱路径的输出整形。与 _ACTION_HANDLERS 的返回结构必须一致——
# 两条路径产出不同形状的结果会让「换成沙箱」变成一次隐蔽的契约变更。
# test_action_sandbox.py 有一个测试断言两者逐键相同。
_RESULT_SHAPERS = {
    "restart_synthetic_service": _shape_restart,
    "rollback_synthetic_deployment": _shape_rollback,
    "throttle_synthetic_traffic": _shape_throttle,
}
