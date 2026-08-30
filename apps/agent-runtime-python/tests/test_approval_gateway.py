"""审批网关的测试。

重点是「这个类不可能批准审批」与「Control Plane 故障不被当成未获批准」。
真实跨服务集成在 scripts/drill-m4.sh 里做，那里连真的 Java 进程。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from agent_runtime.approval.gateway import (
    ControlPlaneApprovalGateway,
    ControlPlaneError,
)

CP = "http://control-plane.test"
ARGS = {"service": "synthetic-orders", "target_version": "v1.4.2"}


def _gateway() -> ControlPlaneApprovalGateway:
    return ControlPlaneApprovalGateway(CP, api_token="test-token-value")


class TestNoApprovePathExists:
    """M0 INV-4 在接口面上的保证。"""

    def test_gateway_has_no_approve_method(self) -> None:
        gateway = _gateway()
        for forbidden in ("approve", "decide", "grant", "authorize"):
            assert not hasattr(gateway, forbidden), (
                f"gateway must not expose {forbidden}(); approval authority lives in Java"
            )

    def test_gateway_only_exposes_three_operations(self) -> None:
        public = {
            name
            for name in dir(_gateway())
            if not name.startswith("_") and callable(getattr(_gateway(), name))
        }
        assert public == {"request", "fetch", "consume"}


class TestTokenIsNotLeaked:
    def test_repr_masks_token(self) -> None:
        gateway = _gateway()
        assert "test-token-value" not in repr(gateway)
        assert "test-token-value" not in str(gateway)
        assert "api_token=***" in repr(gateway)


class TestRequest:
    @respx.mock
    async def test_returns_approval_id(self) -> None:
        respx.post(f"{CP}/api/v1/approvals").mock(
            return_value=httpx.Response(201, json={"approvalId": "apr-1", "decision": "PENDING"})
        )
        approval_id = await _gateway().request(
            run_id="run-1",
            tool_name="rollback_synthetic_deployment",
            resource_ref="svc:synthetic-orders",
            arguments=ARGS,
        )
        assert approval_id == "apr-1"

    @respx.mock
    async def test_sends_bearer_token(self) -> None:
        route = respx.post(f"{CP}/api/v1/approvals").mock(
            return_value=httpx.Response(201, json={"approvalId": "apr-1"})
        )
        await _gateway().request(
            run_id="run-1", tool_name="x", resource_ref="svc:y", arguments={}
        )
        assert route.calls[0].request.headers["authorization"] == "Bearer test-token-value"

    @respx.mock
    async def test_missing_approval_id_is_an_error(self) -> None:
        respx.post(f"{CP}/api/v1/approvals").mock(
            return_value=httpx.Response(201, json={"decision": "PENDING"})
        )
        with pytest.raises(ControlPlaneError, match="approvalId"):
            await _gateway().request(
                run_id="run-1", tool_name="x", resource_ref="svc:y", arguments={}
            )

    @respx.mock
    async def test_400_is_an_error_not_a_denial(self) -> None:
        """请求创建失败是基础设施/参数问题，不是「审批被拒」。"""
        respx.post(f"{CP}/api/v1/approvals").mock(
            return_value=httpx.Response(400, json={"error": "invalid_arguments"})
        )
        with pytest.raises(ControlPlaneError):
            await _gateway().request(
                run_id="run-1", tool_name="x", resource_ref="svc:y", arguments={}
            )


class TestFetch:
    """fetch 走 GET /api/v1/approvals/{id}（M7 新增该端点）。

    在它存在之前 fetch 只能扫 pending 列表，因此对**已决**的审批一律返回 UNKNOWN，
    Policy 无法区分「已批准」与「查不到」，AWAITING_APPROVAL 恢复后永远执行不了动作。
    """

    @respx.mock
    async def test_reads_pending_approval(self) -> None:
        respx.get(f"{CP}/api/v1/approvals/apr-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "approvalId": "apr-1",
                    "toolName": "rollback_synthetic_deployment",
                    "resourceRef": "svc:synthetic-orders",
                    "argumentsDigest": "a" * 64,
                    "decision": "PENDING",
                    "consumedAt": None,
                },
            )
        )
        fact = await _gateway().fetch(approval_id="apr-1")
        assert fact.decision == "PENDING"
        assert fact.consumed is False
        assert fact.arguments_digest == "a" * 64

    @respx.mock
    async def test_reads_decided_approval(self) -> None:
        """已决的审批现在能读到真实决策，不再退化成 UNKNOWN。"""
        respx.get(f"{CP}/api/v1/approvals/apr-2").mock(
            return_value=httpx.Response(
                200,
                json={
                    "approvalId": "apr-2",
                    "toolName": "rollback_synthetic_deployment",
                    "resourceRef": "svc:synthetic-orders",
                    "argumentsDigest": "c" * 64,
                    "decision": "APPROVED",
                    "consumedAt": None,
                },
            )
        )
        fact = await _gateway().fetch(approval_id="apr-2")
        assert fact.decision == "APPROVED"

    @respx.mock
    async def test_rejected_approval_is_not_approved(self) -> None:
        respx.get(f"{CP}/api/v1/approvals/apr-3").mock(
            return_value=httpx.Response(
                200,
                json={
                    "approvalId": "apr-3",
                    "toolName": "x",
                    "resourceRef": "svc:y",
                    "argumentsDigest": "d" * 64,
                    "decision": "REJECTED",
                    "consumedAt": None,
                },
            )
        )
        fact = await _gateway().fetch(approval_id="apr-3")
        assert fact.decision == "REJECTED"
        assert fact.decision != "APPROVED"

    @respx.mock
    async def test_missing_approval_is_unknown_not_approved(self) -> None:
        """404 返回 UNKNOWN 而不是抛异常：审批不存在（可能是伪造的 id）
        是业务事实，让 Policy 拒绝比让整个 Run 崩掉更合适。
        猜「已批准」则会让一个不存在的审批变成放行。"""
        respx.get(f"{CP}/api/v1/approvals/apr-missing").mock(
            return_value=httpx.Response(404, json={"error": "not_found"})
        )
        fact = await _gateway().fetch(approval_id="apr-missing")
        assert fact.decision == "UNKNOWN"
        assert fact.decision != "APPROVED"

    @respx.mock
    async def test_unknown_fact_fails_the_four_way_comparison(self) -> None:
        """UNKNOWN 事实的三个绑定字段必须为空，使 Policy 的比对必然失败。
        「查不到」在授权判定上必须等价于「不放行」，不能等价于「跳过检查」。"""
        respx.get(f"{CP}/api/v1/approvals/apr-x").mock(
            return_value=httpx.Response(404, json={})
        )
        fact = await _gateway().fetch(approval_id="apr-x")
        assert fact.tool_name == ""
        assert fact.resource_ref == ""
        assert fact.arguments_digest == ""

    @respx.mock
    async def test_forbidden_is_unknown(self) -> None:
        """403 表示这个审批不属于当前租户。同样退化为 UNKNOWN。"""
        respx.get(f"{CP}/api/v1/approvals/apr-other-tenant").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        fact = await _gateway().fetch(approval_id="apr-other-tenant")
        assert fact.decision == "UNKNOWN"

    @respx.mock
    async def test_consumed_approval_is_flagged(self) -> None:
        respx.get(f"{CP}/api/v1/approvals/apr-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "approvalId": "apr-1",
                    "toolName": "x",
                    "resourceRef": "svc:y",
                    "argumentsDigest": "b" * 64,
                    "decision": "APPROVED",
                    "consumedAt": "2026-08-27T12:00:00Z",
                },
            )
        )
        fact = await _gateway().fetch(approval_id="apr-1")
        assert fact.consumed is True

    @respx.mock
    async def test_unexpected_status_raises(self) -> None:
        """500 不是业务事实而是控制面坏了。静默返回 UNKNOWN 会让
        「控制面挂了」被当成「审批不存在」。"""
        respx.get(f"{CP}/api/v1/approvals/apr-1").mock(
            return_value=httpx.Response(500, text="boom")
        )
        with pytest.raises(ControlPlaneError):
            await _gateway().fetch(approval_id="apr-1")


class TestConsume:
    @respx.mock
    async def test_200_means_allowed(self) -> None:
        respx.post(f"{CP}/api/v1/approvals/apr-1/consume").mock(
            return_value=httpx.Response(200, json={"approvalId": "apr-1"})
        )
        assert await _gateway().consume(
            approval_id="apr-1",
            tool_name="rollback_synthetic_deployment",
            resource_ref="svc:synthetic-orders",
            arguments=ARGS,
        ) is True

    @respx.mock
    @pytest.mark.parametrize("status", [403, 404, 409])
    async def test_denials_return_false_without_raising(self, status: int) -> None:
        """明确的拒绝是正常业务结果，不是异常。"""
        respx.post(f"{CP}/api/v1/approvals/apr-1/consume").mock(
            return_value=httpx.Response(status, json={"error": "approval_verification_failed"})
        )
        assert await _gateway().consume(
            approval_id="apr-1", tool_name="x", resource_ref="svc:y", arguments={}
        ) is False

    @respx.mock
    async def test_500_raises_rather_than_denying(self) -> None:
        """Control Plane 挂了不等于「未获批准」。

        把两者混成一个返回值会让「审批系统故障」被当成「动作未获批准」而静默继续——
        更糟的是反向：如果 500 被当成 True，就是未审批执行。
        """
        respx.post(f"{CP}/api/v1/approvals/apr-1/consume").mock(
            return_value=httpx.Response(500, json={"error": "internal_error"})
        )
        with pytest.raises(ControlPlaneError, match="unexpected status 500"):
            await _gateway().consume(
                approval_id="apr-1", tool_name="x", resource_ref="svc:y", arguments={}
            )

    @respx.mock
    async def test_network_failure_raises(self) -> None:
        respx.post(f"{CP}/api/v1/approvals/apr-1/consume").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(ControlPlaneError, match="consume call failed"):
            await _gateway().consume(
                approval_id="apr-1", tool_name="x", resource_ref="svc:y", arguments={}
            )

    @respx.mock
    async def test_consume_sends_all_four_fields(self) -> None:
        """Java 侧比对 toolName / resourceRef / argumentsDigest / 未过期未消费。
        少传一项就等于少比一项。"""
        route = respx.post(f"{CP}/api/v1/approvals/apr-1/consume").mock(
            return_value=httpx.Response(200, json={})
        )
        await _gateway().consume(
            approval_id="apr-1",
            tool_name="rollback_synthetic_deployment",
            resource_ref="svc:synthetic-orders",
            arguments=ARGS,
        )
        import json as _json

        body = _json.loads(route.calls[0].request.content)
        assert body["toolName"] == "rollback_synthetic_deployment"
        assert body["resourceRef"] == "svc:synthetic-orders"
        assert body["arguments"] == ARGS

    @respx.mock
    async def test_error_body_is_redacted(self) -> None:
        respx.post(f"{CP}/api/v1/approvals/apr-1/consume").mock(
            return_value=httpx.Response(
                502, text="upstream rejected token=sk-abcdefghijklmnopqrstuv"
            )
        )
        with pytest.raises(ControlPlaneError) as exc:
            await _gateway().consume(
                approval_id="apr-1", tool_name="x", resource_ref="svc:y", arguments={}
            )
        assert "sk-abcdefghijklmnopqrstuv" not in str(exc.value)


class TestGatewaySatisfiesTheProtocol:
    def test_it_can_be_used_where_approval_gateway_is_expected(self) -> None:
        """结构化子类型检查：真实网关与测试替身可互换。"""
        gateway = _gateway()
        for method in ("request", "fetch", "consume"):
            assert callable(getattr(gateway, method))
