"""只读工具的执行层。

三段分离的第三段（Executor）。它**必须**持有 ToolAuthorization 才能执行——
签名上要求它，因此绕过 Policy 直接调用在类型层面就不成立。

同时是 direct transport：MCP 是可替换的传输层（ADR-0007 §3），两者共用这一层。
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

# 当前调用的 tenant。用 contextvar 而不是加进 handler 签名：
# 所有 handler 共用一个签名便于注册表管理，而 tenant 绝不能从工具参数读取
# ——那是客户端自报的不可信输入（威胁 T-4）。
_current_tenant: ContextVar[str] = ContextVar("current_tenant", default="")

import httpx

from ..tools.catalogue import BY_NAME
from ..tools.contract import ToolAuthorization, ToolContract


class ToolFailure(Exception):
    """typed failure。failure_code 必须在契约声明的 typed_failures 集合内——
    未分类的失败会让 M6 的归因统计出现吞掉一切的黑洞。"""

    def __init__(self, failure_code: str, message: str, *, tool_name: str) -> None:
        super().__init__(f"{tool_name}: {failure_code}: {message}")
        self.failure_code = failure_code
        self.tool_name = tool_name

    def validate_against(self, contract: ToolContract) -> None:
        if self.failure_code not in contract.typed_failures:
            raise AssertionError(
                f"{contract.name} raised undeclared failure {self.failure_code!r}; "
                f"declared: {sorted(contract.typed_failures)}"
            )


@dataclass(frozen=True)
class ToolResult:
    tool_name: str
    payload: dict[str, Any]
    result_bytes: int
    # 只读工具的结果是不可信数据（M0 §9 信任分级）。这个标记随结果一路传到
    # Prompt 构造层，使「日志是数据不是指令」在代码里可见而不只是文档主张。
    untrusted: bool = True


class ReadOnlyToolExecutor:
    """只读工具执行器。

    只读工具进程内执行（说明书 §18 Sandbox 第一阶段），取消是协作式的
    （ADR-0001 C-6）：超时后放弃等待并标 typed failure，不假装能硬取消。

    retrieval 为 None 时 retrieve_runbook_section 返回未命中而不是报错：
    检索未接入是一种可观测的状态，不是故障。S7 的正确弃答在两种情况下都成立。
    """

    def __init__(
        self,
        lab_base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        retrieval: Any | None = None,
    ) -> None:
        self.lab_base_url = lab_base_url.rstrip("/")
        self._client = client
        self._retrieval = retrieval

    async def execute(self, authorization: ToolAuthorization) -> ToolResult:
        if not authorization.allowed:
            # 走到这里说明调用方拿着一个被拒的授权还在执行——是代码缺陷，不是运行时状况。
            raise AssertionError(
                f"executor received a denied authorization for {authorization.tool_name}"
            )
        contract = BY_NAME.get(authorization.tool_name)
        if contract is None:
            raise ToolFailure(
                "invalid_arguments", "tool not in catalogue", tool_name=authorization.tool_name
            )
        if contract.is_write():
            raise AssertionError(
                f"{contract.name} is a write tool; ReadOnlyToolExecutor must not run it"
            )

        handler = _HANDLERS.get(contract.name)
        if handler is None:
            raise ToolFailure(
                "invalid_arguments", "no handler registered", tool_name=contract.name
            )

        token = _current_tenant.set(authorization.binding.tenant_id)
        try:
            payload = await asyncio.wait_for(
                handler(self, contract, authorization.arguments),
                timeout=contract.timeout_seconds,
            )
        except TimeoutError as exc:
            # 协作式取消的现实：我们放弃等待，但底层协程可能仍在跑到自然结束。
            # 缓解是工具自身的 HTTP timeout 小于这个值 + 结果大小上限。
            raise ToolFailure(
                "tool_timeout",
                f"exceeded {contract.timeout_seconds}s; abandoning wait (cooperative cancel)",
                tool_name=contract.name,
            ) from exc
        finally:
            _current_tenant.reset(token)

        encoded = json.dumps(payload, ensure_ascii=False).encode()
        if len(encoded) > contract.max_result_bytes:
            raise ToolFailure(
                "result_too_large",
                f"{len(encoded)} bytes exceeds limit {contract.max_result_bytes}",
                tool_name=contract.name,
            )
        return ToolResult(
            tool_name=contract.name, payload=payload, result_bytes=len(encoded)
        )

    # -- synthetic-lab 访问 ------------------------------------------------

    async def _get(self, contract: ToolContract, path: str, params: dict[str, Any]) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}
        client = self._client or httpx.AsyncClient(
            # 工具自身的 timeout 必须小于契约 timeout，否则协作式取消永远来不及生效。
            timeout=max(1.0, contract.timeout_seconds - 2.0)
        )
        owns = self._client is None
        try:
            response = await client.get(f"{self.lab_base_url}{path}", params=clean)
        except httpx.TimeoutException as exc:
            raise ToolFailure("tool_timeout", str(exc), tool_name=contract.name) from exc
        except httpx.HTTPError as exc:
            raise ToolFailure(
                "upstream_unavailable", str(exc), tool_name=contract.name
            ) from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code == 404:
            body = _safe_json(response)
            code = body.get("error", "")
            mapped = {
                "unknown_service": "unknown_service",
                "unknown_metric": "unknown_metric",
                "unknown_queue": "unknown_queue",
            }.get(code, "upstream_unavailable")
            raise ToolFailure(mapped, body.get("message", "not found"), tool_name=contract.name)
        if response.status_code == 422:
            raise ToolFailure(
                "invalid_arguments",
                _safe_json(response).get("message", "invalid parameters"),
                tool_name=contract.name,
            )
        if response.status_code >= 400:
            raise ToolFailure(
                "upstream_unavailable",
                f"synthetic-lab returned {response.status_code}",
                tool_name=contract.name,
            )
        return _safe_json(response)


def _safe_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# -- 各工具的处理函数 -------------------------------------------------------


async def _get_service_metrics(
    executor: ReadOnlyToolExecutor, contract: ToolContract, args: dict[str, Any]
) -> dict:
    metrics = await executor._get(
        contract,
        "/v1/metrics",
        {
            "service": args["service"],
            "metric": args["metric"],
            "window_minutes": args.get("window_minutes"),
            "offset_minutes": args.get("offset_minutes"),
        },
    )
    payload = {
        "service": metrics.get("service", args["service"]),
        "metric": metrics.get("metric", args["metric"]),
        "unit": metrics.get("unit", ""),
        "coverage": metrics.get("coverage", "covered"),
        "points": metrics.get("points", []),
    }
    # 容器事件并入本工具而非新增第 6 个工具（ADR-0001 C-5 的折中）。
    if args.get("include_runtime_events", True):
        events = await executor._get(
            contract,
            "/v1/runtime-events",
            {
                "service": args["service"],
                "window_minutes": args.get("window_minutes"),
                "offset_minutes": args.get("offset_minutes"),
            },
        )
        payload["runtime_events"] = [
            {
                "event_type": e.get("event_type", ""),
                "timestamp": e.get("timestamp", ""),
                "restart_count": e.get("restart_count", 0),
                "exit_code": e.get("exit_code"),
                "reason": e.get("reason", ""),
            }
            for e in events.get("events", [])
        ]
    return payload


async def _search_service_logs(
    executor: ReadOnlyToolExecutor, contract: ToolContract, args: dict[str, Any]
) -> dict:
    body = await executor._get(
        contract,
        "/v1/logs",
        {
            "service": args["service"],
            "query": args.get("query"),
            "limit": args.get("limit", 100),
            "window_minutes": args.get("window_minutes"),
            "offset_minutes": args.get("offset_minutes"),
        },
    )
    return {
        "service": body.get("service", args["service"]),
        "coverage": body.get("coverage", "covered"),
        "entries": [
            {
                "timestamp": e.get("timestamp", ""),
                "level": e.get("level", "INFO"),
                "message": e.get("message", ""),
                # 恒为 true。日志内容永远是不可信数据，不因来源而改变。
                "untrusted": True,
            }
            for e in body.get("entries", [])
        ],
    }


async def _get_recent_deployments(
    executor: ReadOnlyToolExecutor, contract: ToolContract, args: dict[str, Any]
) -> dict:
    body = await executor._get(contract, "/v1/deployments", {"service": args["service"]})
    return {
        "service": body.get("service", args["service"]),
        "deployments": [
            {
                "version": d.get("version", ""),
                "previous_version": d.get("previous_version", ""),
                "deployed_at": d.get("deployed_at", ""),
                # 只有键名。配置值可能是凭据（威胁 T-3）。
                "changed_config_keys": list(d.get("changed_config_keys", [])),
                # is_decoy 在这里被剥除：它是 grader 用的标记，
                # 透传给 Agent 等于把 S2 的答案交给被试
                # （M2.5 Gate 报告 §5.1 第 5 条）。字段白名单是这条保证的实现方式。
            }
            for d in body.get("deployments", [])
        ],
    }


async def _get_queue_state(
    executor: ReadOnlyToolExecutor, contract: ToolContract, args: dict[str, Any]
) -> dict:
    body = await executor._get(
        contract,
        "/v1/queues",
        {
            "queue": args["queue"],
            "window_minutes": args.get("window_minutes"),
            "offset_minutes": args.get("offset_minutes"),
        },
    )
    return {
        "queue": body.get("queue", args["queue"]),
        "consumer_count": body.get("consumer_count", 0),
        "coverage": body.get("coverage", "covered"),
        "points": body.get("points", []),
    }


async def _retrieve_runbook_section(
    executor: ReadOnlyToolExecutor, contract: ToolContract, args: dict[str, Any]
) -> dict:
    """Runbook 检索。

    retrieval_hit 是显式字段而不是 len(sections) > 0：空结果有两种含义
    ——「查了但没有」与「检索未接入」。S7（Runbook 缺失）的正确弃答依赖
    「查了但没有」这个事实可被观测。
    """
    service = executor._retrieval
    if service is None:
        return {"retrieval_hit": False, "sections": []}

    # tenant 从 binding 派生，不从工具参数读（威胁 T-4）。
    tenant_id = _current_tenant.get()
    outcome = service.retrieve(
        args["symptom"],
        tenant_id=tenant_id,
        service=args.get("service"),
        top_k=int(args.get("top_k", 3)),
    )
    from .. import observability as obs

    obs.retrieval_queries.labels("true" if outcome.hit else "false").inc()
    return {
        "retrieval_hit": outcome.hit,
        "sections": [
            {
                "document_id": hit.chunk.document_id,
                "document_version": hit.chunk.document_version,
                "section_id": hit.chunk.section_id,
                "service": hit.chunk.service,
                "content": hit.chunk.content,
                "content_hash": hit.chunk.content_hash,
                "relevance": round(hit.score, 4),
                # Runbook 是版本化数据，不是系统指令（M0 §9 信任分级）。
                "untrusted": True,
            }
            for hit in outcome.sections
        ],
    }


_HANDLERS = {
    "get_service_metrics": _get_service_metrics,
    "search_service_logs": _search_service_logs,
    "get_recent_deployments": _get_recent_deployments,
    "get_queue_state": _get_queue_state,
    "retrieve_runbook_section": _retrieve_runbook_section,
}
