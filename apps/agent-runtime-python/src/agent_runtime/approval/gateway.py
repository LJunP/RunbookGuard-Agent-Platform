"""接 Java Control Plane 的真实审批网关。

M0 INV-4 的实现：这个类**只有** request / fetch / consume 三个方法，没有 approve。
Python 侧不存在能批准审批的代码路径——不是靠约定，是靠这个类的接口面。

consume 是执行前的最后一道闸：它把四项比对交给 Java 侧重做一次。Policy 已经比过
一次，两道都要，因为 Policy 到 Executor 之间仍有一个时间窗口，而审批可能在那期间
被撤销或被另一个 Worker 消费掉。
"""

from __future__ import annotations

from typing import Any

import httpx

from ..redaction import redact
from ..tools.policy import ApprovalFact


class ControlPlaneError(RuntimeError):
    """Control Plane 不可用或返回了无法解释的响应。

    区别于「审批被拒绝」：后者是正常的业务结果，前者是基础设施故障。
    把两者混成一个异常会让「审批系统挂了」被当成「动作未获批准」而静默继续。
    """


class ControlPlaneApprovalGateway:
    def __init__(
        self,
        base_url: str,
        *,
        api_token: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # token 只从构造参数传入，不落任何文件。repr 不暴露它。
        self._token = api_token
        self._client = client
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return f"ControlPlaneApprovalGateway(base_url={self.base_url!r}, api_token=***)"

    __str__ = __repr__

    async def request(
        self, *, run_id: str, tool_name: str, resource_ref: str, arguments: dict[str, Any]
    ) -> str:
        body = await self._post(
            "/api/v1/approvals",
            {
                "runId": run_id,
                "toolName": tool_name,
                "resourceRef": resource_ref,
                "arguments": arguments,
            },
            expected=(201,),
        )
        approval_id = body.get("approvalId")
        if not approval_id:
            raise ControlPlaneError("control plane did not return an approvalId")
        return approval_id

    async def fetch(self, *, approval_id: str) -> ApprovalFact:
        """读取审批事实。

        走 GET /api/v1/approvals/{id}（M7 新增）。在它存在之前只能扫 pending 列表，
        因此对**已决**的审批一律返回 UNKNOWN，Policy 于是无法区分「已批准」与
        「查不到」，只能保守拒绝——AWAITING_APPROVAL 恢复后永远执行不了动作。

        读到 APPROVED 不等于可以执行：放行仍要求 consume 的四项比对（M0 INV-4）。

        404 返回 UNKNOWN 而不是抛异常：审批不存在是一个业务事实
        （可能是伪造的 id），让 Policy 拒绝比让整个 Run 崩掉更合适。
        """
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns = self._client is None
        try:
            response = await client.get(
                f"{self.base_url}/api/v1/approvals/{approval_id}",
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ControlPlaneError(f"fetch call failed: {redact(str(exc))}") from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code == 200:
            return _to_fact(response.json())
        if response.status_code in (403, 404):
            return _unknown_fact(approval_id)
        raise ControlPlaneError(
            f"unexpected status {response.status_code} from fetch: "
            f"{redact(response.text[:200])}"
        )

    async def consume(
        self, *, approval_id: str, tool_name: str, resource_ref: str, arguments: dict[str, Any]
    ) -> bool:
        """执行前的最后一道闸。

        Java 侧比对 toolName / resourceRef / argumentsDigest / 未过期未消费，
        任一不符返回 403。返回 True 表示审批已被消费且可以执行——
        这个副作用是刻意的：同一份审批不能用两次（防重放）。
        """
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns = self._client is None
        try:
            response = await client.post(
                f"{self.base_url}/api/v1/approvals/{approval_id}/consume",
                json={
                    "toolName": tool_name,
                    "resourceRef": resource_ref,
                    "arguments": arguments,
                },
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ControlPlaneError(
                f"consume call failed: {redact(str(exc))}"
            ) from exc
        finally:
            if owns:
                await client.aclose()

        if response.status_code == 200:
            return True
        if response.status_code in (403, 404, 409):
            # 明确的拒绝。不抛异常：这是正常的业务结果，调用方据此落 authorization_failed。
            return False
        raise ControlPlaneError(
            f"unexpected status {response.status_code} from consume: "
            f"{redact(response.text[:200])}"
        )

    # -- HTTP --------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}", "content-type": "application/json"}

    async def _post(self, path: str, body: dict, *, expected: tuple[int, ...]) -> dict:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns = self._client is None
        try:
            response = await client.post(
                f"{self.base_url}{path}", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise ControlPlaneError(f"{path} failed: {redact(str(exc))}") from exc
        finally:
            if owns:
                await client.aclose()
        if response.status_code not in expected:
            raise ControlPlaneError(
                f"{path} returned {response.status_code}: {redact(response.text[:200])}"
            )
        return _safe_json(response)


def _unknown_fact(approval_id: str) -> ApprovalFact:
    """审批不存在或不可见时的事实。

    tool_name / resource_ref / digest 全空使 Policy 的四项比对必然失败——
    「查不到」在授权判定上必须等价于「不放行」，不能等价于「跳过检查」。
    """
    return ApprovalFact(
        approval_id=approval_id,
        tool_name="",
        resource_ref="",
        arguments_digest="",
        decision="UNKNOWN",
        consumed=False,
    )


def _to_fact(item: dict) -> ApprovalFact:
    return ApprovalFact(
        approval_id=item["approvalId"],
        tool_name=item.get("toolName", ""),
        resource_ref=item.get("resourceRef", ""),
        arguments_digest=item.get("argumentsDigest", ""),
        decision=item.get("decision", "UNKNOWN"),
        consumed=bool(item.get("consumedAt")),
    )


def _safe_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
