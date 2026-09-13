"""有界执行循环的端到端测试（不含 LangGraph）。

M4 Gate 的硬条件在这里被证明：未审批的写动作 100% 不执行、审批后篡改参数被拦、
AWAITING_APPROVAL 可挂起。

审批网关用测试替身，但它的行为刻意做成「Java 侧会怎么做」——包括
consume 的四项比对。用替身是因为跨进程集成在 test_control_plane_integration 里另测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from agent_runtime.agent.bounded_loop import BoundedAgentLoop, RunSpec
from agent_runtime.agent.state import RunStatus
from agent_runtime.agent.termination import BoundedLoopGuard, BudgetState, state_fingerprint
from agent_runtime.approval.digest import digest as digest_fn
from agent_runtime.provider.fake import FakeProvider, FakeTurn
from agent_runtime.tools import catalogue
from agent_runtime.tools.contract import ToolSuggestion
from agent_runtime.tools.executor import ReadOnlyToolExecutor
from agent_runtime.tools.policy import ApprovalFact, PolicyEngine

LAB = "http://lab.test"
T0 = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

VALID_DIAGNOSIS = json.dumps(
    {
        "conclusion_type": "diagnosis",
        "root_cause": "connection pool exhausted after v1.5.0",
        "confidence": "high",
        "root_cause_service": "synthetic-orders",
        "claims": [
            {
                "statement": "the pool saturated shortly after the v1.5.0 deploy",
                "evidence_ids": ["ev-1"],
            }
        ],
        "ruled_out": [],
        "conflicting_signals": [],
        "missing_evidence": [],
        "proposed_action": None,
        "recommended_next_step": "roll back to v1.4.2",
    }
)


@dataclass
class StubApprovalGateway:
    """测试替身。行为按 Java 侧的语义实现，包括 consume 的四项比对。

    刻意**没有** approve 方法：Python 侧不存在能批准审批的代码路径（M0 INV-4）。
    测试要模拟「人批准了」时直接改 decision 字段，那是外部事件而非 Runtime 的能力。
    """

    decision: str = "PENDING"
    consumed: bool = False
    # 模拟审批后被篡改：审批记录里绑定的参数与执行时传入的不同。
    approved_arguments: dict[str, Any] | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)
    consume_calls: list[dict[str, Any]] = field(default_factory=list)
    _tool_name: str = ""
    _resource_ref: str = ""

    async def request(self, *, run_id, tool_name, resource_ref, arguments) -> str:
        self.requests.append(
            {"run_id": run_id, "tool_name": tool_name, "resource_ref": resource_ref,
             "arguments": dict(arguments)}
        )
        self._tool_name = tool_name
        self._resource_ref = resource_ref
        if self.approved_arguments is None:
            self.approved_arguments = dict(arguments)
        return "apr-stub-1"

    async def fetch(self, *, approval_id) -> ApprovalFact:
        return ApprovalFact(
            approval_id=approval_id,
            tool_name=self._tool_name,
            resource_ref=self._resource_ref,
            arguments_digest=digest_fn(self.approved_arguments or {}),
            decision=self.decision,
            consumed=self.consumed,
        )

    async def consume(self, *, approval_id, tool_name, resource_ref, arguments) -> bool:
        self.consume_calls.append({"approval_id": approval_id, "arguments": dict(arguments)})
        if self.decision != "APPROVED" or self.consumed:
            return False
        if tool_name != self._tool_name or resource_ref != self._resource_ref:
            return False
        # 四项比对的第四项：参数摘要。
        if digest_fn(arguments) != digest_fn(self.approved_arguments or {}):
            return False
        self.consumed = True
        return True


def _budget(**overrides) -> BudgetState:
    defaults = dict(
        max_steps=60,
        deadline=T0 + timedelta(minutes=10),
        cost_budget_micros=500_000,
        token_budget=200_000,
        tool_call_budget=20,
    )
    defaults.update(overrides)
    return BudgetState(**defaults)


def _spec(**overrides) -> RunSpec:
    defaults = dict(
        run_id="run-test-1",
        tenant_id="tenant-demo",
        principal_id="prin-agent",
        incident_summary="synthetic-orders p99 above 3s",
        allowed_tool_names=frozenset(t.name for t in catalogue.READ_ONLY_TOOLS),
        permitted_resources=frozenset({"svc:synthetic-orders", "queue:synthetic-notify.work"}),
    )
    defaults.update(overrides)
    return RunSpec(**defaults)


def _loop(
    *,
    provider=None,
    approvals=None,
    budget: BudgetState | None = None,
    now: datetime = T0,
    client: httpx.AsyncClient | None = None,
    **guard_flags,
) -> BoundedAgentLoop:
    return BoundedAgentLoop(
        provider=provider or FakeProvider(script=[FakeTurn(text=VALID_DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB, client=client),
        guard=BoundedLoopGuard(budget or _budget(), now=lambda: now),
        approvals=approvals,
        **guard_flags,
    )


def _metrics_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "service": "synthetic-orders",
            "metric": "db_pool_active",
            "unit": "connections",
            "coverage": "covered",
            "points": [{"timestamp": "2026-08-27T11:30:00+00:00", "value": 12.0}],
        },
    )


def _events_response() -> httpx.Response:
    return httpx.Response(200, json={"service": "synthetic-orders", "coverage": "covered", "events": []})


def _mount_lab() -> None:
    respx.get(f"{LAB}/v1/metrics").mock(return_value=_metrics_response())
    respx.get(f"{LAB}/v1/runtime-events").mock(return_value=_events_response())
    respx.get(f"{LAB}/v1/logs").mock(
        return_value=httpx.Response(
            200,
            json={
                "service": "synthetic-orders",
                "coverage": "covered",
                "entries": [
                    {
                        "timestamp": "2026-08-27T12:01:00+00:00",
                        "level": "ERROR",
                        "message": "connection pool exhausted",
                        "injected": False,
                    }
                ],
            },
        )
    )
    respx.get(f"{LAB}/v1/deployments").mock(
        return_value=httpx.Response(
            200,
            json={
                "service": "synthetic-orders",
                "deployments": [
                    {
                        "service": "synthetic-orders",
                        "version": "v1.5.0",
                        "previous_version": "v1.4.2",
                        "deployed_at": "2026-08-27T11:50:00+00:00",
                        "changed_config_keys": ["orders.repository.connectionLeaseMode"],
                        "is_decoy": True,
                    }
                ],
            },
        )
    )


METRICS_CALL = ToolSuggestion(
    tool_name="get_service_metrics",
    arguments={"service": "synthetic-orders", "metric": "db_pool_active"},
)
LOGS_CALL = ToolSuggestion(
    tool_name="search_service_logs",
    arguments={"service": "synthetic-orders", "query": "connection pool"},
)
ROLLBACK_CALL = ToolSuggestion(
    tool_name="rollback_synthetic_deployment",
    arguments={"service": "synthetic-orders", "target_version": "v1.4.2"},
)


class TestHappyPath:
    @respx.mock
    async def test_read_only_run_reaches_complete(self) -> None:
        _mount_lab()
        loop = _loop()
        outcome = await loop.run(_spec(), [METRICS_CALL, LOGS_CALL])
        assert outcome.terminal_status is RunStatus.COMPLETE
        assert outcome.failure_class is None
        assert outcome.diagnosis["conclusion_type"] == "diagnosis"
        assert len(outcome.evidence) == 2

    @respx.mock
    async def test_evidence_is_marked_untrusted(self) -> None:
        """M0 §9 的信任分级要在数据里可见，不只是文档主张。"""
        _mount_lab()
        outcome = await _loop().run(_spec(), [LOGS_CALL])
        assert all(e.untrusted for e in outcome.evidence)

    @respx.mock
    async def test_steps_are_recorded_in_order(self) -> None:
        _mount_lab()
        outcome = await _loop().run(_spec(), [METRICS_CALL])
        nodes = [s.node for s in outcome.steps]
        assert RunStatus.POLICY_CHECK in nodes
        assert nodes.index(RunStatus.POLICY_CHECK) < nodes.index(RunStatus.EXECUTING_TOOL)
        assert [s.sequence for s in outcome.steps] == list(range(1, len(outcome.steps) + 1))


class TestIsDecoyStripping:
    @respx.mock
    async def test_decoy_marker_never_reaches_the_agent(self) -> None:
        """synthetic-lab 返回 is_decoy（grader 用的标记）。透传给 Agent
        等于把 S2 的答案交给被试（M2.5 Gate 报告 §5.1 第 5 条）。"""
        _mount_lab()
        executor = ReadOnlyToolExecutor(LAB)
        from agent_runtime.tools.contract import ResourceBinding, issue_authorization

        auth = issue_authorization(
            tool_name="get_recent_deployments",
            binding=ResourceBinding("p", "t", "svc:synthetic-orders"),
            arguments={"service": "synthetic-orders"},
            arguments_digest="a" * 64,
            allowed=True,
            reason="test",
            requires_approval=False,
        )
        result = await executor.execute(auth)
        serialised = json.dumps(result.payload)
        assert "is_decoy" not in serialised
        assert result.payload["deployments"][0]["version"] == "v1.5.0"


class TestWriteActionRequiresApproval:
    """M4 Gate 硬条件。"""

    @respx.mock
    async def test_write_without_approval_gateway_is_denied(self) -> None:
        _mount_lab()
        loop = _loop(approvals=None)
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))
        outcome = await loop.run(spec, [ROLLBACK_CALL])
        # 没有审批 → Policy 拒绝 → 无证据 → 安全停止
        denials = [s for s in outcome.steps if s.failure_code == "approval_required_but_absent"]
        assert denials, "写动作在无审批时必须被拒绝"
        assert outcome.diagnosis["conclusion_type"] == "insufficient_evidence"

    @respx.mock
    async def test_pending_approval_suspends_the_run(self) -> None:
        """AWAITING_APPROVAL 是一个可持久化的挂起点，不是失败。"""
        _mount_lab()
        gateway = StubApprovalGateway(decision="PENDING")
        loop = _loop(approvals=gateway)
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))
        outcome = await loop.run(spec, [ROLLBACK_CALL])
        assert outcome.terminal_status is RunStatus.AWAITING_APPROVAL
        assert outcome.awaiting_approval_id == "apr-stub-1"
        assert gateway.consume_calls == [], "未批准时不得尝试消费审批"

    @respx.mock
    async def test_rejected_approval_never_executes(self) -> None:
        _mount_lab()
        gateway = StubApprovalGateway(decision="REJECTED")
        loop = _loop(approvals=gateway)
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))
        outcome = await loop.run(spec, [ROLLBACK_CALL])
        assert gateway.consume_calls == []
        assert outcome.terminal_status is not RunStatus.COMPLETE or not outcome.evidence

    @respx.mock
    async def test_tampered_arguments_are_refused_by_control_plane(self) -> None:
        """威胁 T-2 的最后一道闸：Policy 通过后，Java 侧的 consume 仍会拒绝。

        构造方式：审批记录里绑定的是 synthetic-orders，执行时传 synthetic-db。
        """
        _mount_lab()
        gateway = StubApprovalGateway(
            decision="APPROVED",
            approved_arguments={"service": "synthetic-orders", "target_version": "v1.4.2"},
        )
        # Policy 那一层用的 approval fact 摘要与建议一致（模拟「审批时看到的是这个」），
        # 但真正执行时参数被改。这里通过让 gateway 记住不同参数来模拟。
        gateway.approved_arguments = {"service": "synthetic-db", "target_version": "v1.4.2"}
        loop = _loop(approvals=gateway)
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))
        outcome = await loop.run(spec, [ROLLBACK_CALL])
        # Policy 层的 digest 比对已经会拦住（approval_digest_mismatch）
        codes = {s.failure_code for s in outcome.steps}
        assert "approval_digest_mismatch" in codes


class TestTerminationInLoop:
    @respx.mock
    async def test_tool_call_budget_terminates_run(self) -> None:
        _mount_lab()
        loop = _loop(budget=_budget(tool_call_budget=1))
        outcome = await loop.run(_spec(), [METRICS_CALL, LOGS_CALL])
        assert outcome.terminal_status is RunStatus.FAILED
        assert outcome.failure_class == "tool_call_budget_exhausted"

    @respx.mock
    async def test_max_steps_terminates_run(self) -> None:
        _mount_lab()
        loop = _loop(budget=_budget(max_steps=2))
        outcome = await loop.run(_spec(), [METRICS_CALL])
        assert outcome.terminal_status is RunStatus.FAILED
        assert outcome.failure_class == "max_steps_exhausted"

    @respx.mock
    async def test_expired_deadline_terminates_immediately(self) -> None:
        _mount_lab()
        loop = _loop(budget=_budget(deadline=T0 - timedelta(seconds=1)))
        outcome = await loop.run(_spec(), [METRICS_CALL])
        assert outcome.failure_class == "deadline_exceeded"

    @respx.mock
    async def test_version_incompatible_terminates(self) -> None:
        """M0 终止条件 7：不兼容就走失败终态，不硬恢复。"""
        _mount_lab()
        loop = _loop(version_incompatible=True)
        outcome = await loop.run(_spec(), [METRICS_CALL])
        assert outcome.failure_class == "version_incompatible"

    @respx.mock
    async def test_safety_rule_terminates(self) -> None:
        _mount_lab()
        loop = _loop(safety_rule_hit=True)
        outcome = await loop.run(_spec(), [METRICS_CALL])
        assert outcome.failure_class == "safety_rule_triggered"

    @respx.mock
    async def test_repeated_state_terminates(self) -> None:
        """同一个工具建议重复三次 → 处境未变 → 判定循环。"""
        _mount_lab()
        loop = _loop()
        outcome = await loop.run(_spec(), [METRICS_CALL] * 4)
        assert outcome.failure_class == "repeated_state_detected"


class TestSafeStop:
    @respx.mock
    async def test_no_evidence_gives_insufficient_evidence(self) -> None:
        """S7/S8：证据不足时不编造根因。"""
        _mount_lab()
        outcome = await _loop().run(_spec(), [])
        assert outcome.terminal_status is RunStatus.COMPLETE
        assert outcome.diagnosis["conclusion_type"] == "insufficient_evidence"
        assert outcome.diagnosis["missing_evidence"]

    @respx.mock
    async def test_runbook_miss_is_observable(self) -> None:
        """检索未命中是事实，必须可观测——静默返回空数组会让 S7 无法判定。"""
        _mount_lab()
        loop = _loop()
        spec = _spec(allowed_tool_names=frozenset({"retrieve_runbook_section"}))
        suggestion = ToolSuggestion(
            tool_name="retrieve_runbook_section",
            arguments={"symptom": "pool exhausted"},
        )
        outcome = await loop.run(spec, [suggestion])
        assert outcome.terminal_status is RunStatus.COMPLETE


class TestModelFailureDoesNotBecomeSuccess:
    @respx.mock
    async def test_garbage_diagnosis_terminates_as_failure(self) -> None:
        """M3 Gate 条件在 Agent 层同样成立：模型胡说不能表现为成功。"""
        _mount_lab()
        loop = _loop(provider=FakeProvider(script=[FakeTurn(text="the database is fine")]))
        outcome = await loop.run(_spec(), [METRICS_CALL])
        assert outcome.terminal_status is RunStatus.FAILED
        assert outcome.failure_class == "provider_failure"
        assert outcome.diagnosis is None

    @respx.mock
    async def test_provider_timeout_terminates_as_failure(self) -> None:
        from agent_runtime.provider.errors import ProviderTimeout

        _mount_lab()
        loop = _loop(provider=FakeProvider(script=[FakeTurn(raise_=ProviderTimeout())]))
        outcome = await loop.run(_spec(), [METRICS_CALL])
        assert outcome.terminal_status is RunStatus.FAILED
        assert outcome.failure_class == "provider_failure"


class TestToolFailureHandling:
    @respx.mock
    async def test_unknown_service_is_typed_failure_not_crash(self) -> None:
        respx.get(f"{LAB}/v1/metrics").mock(
            return_value=httpx.Response(
                404, json={"error": "unknown_service", "message": "no such service"}
            )
        )
        respx.get(f"{LAB}/v1/runtime-events").mock(return_value=_events_response())
        loop = _loop()
        outcome = await loop.run(_spec(), [METRICS_CALL])
        codes = {s.failure_code for s in outcome.steps}
        assert "unknown_service" in codes

    @respx.mock
    async def test_upstream_down_is_typed_failure(self) -> None:
        respx.get(f"{LAB}/v1/metrics").mock(
            return_value=httpx.Response(503, json={"error": "down"})
        )
        respx.get(f"{LAB}/v1/runtime-events").mock(return_value=_events_response())
        outcome = await _loop().run(_spec(), [METRICS_CALL])
        codes = {s.failure_code for s in outcome.steps}
        assert "upstream_unavailable" in codes

    @respx.mock
    async def test_tool_failure_does_not_abort_the_whole_run(self) -> None:
        """一个工具失败后应继续用其它工具取证，而不是整体崩掉。"""
        respx.get(f"{LAB}/v1/metrics").mock(
            return_value=httpx.Response(503, json={"error": "down"})
        )
        respx.get(f"{LAB}/v1/runtime-events").mock(return_value=_events_response())
        respx.get(f"{LAB}/v1/logs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "service": "synthetic-orders",
                    "coverage": "covered",
                    "entries": [
                        {"timestamp": "t", "level": "ERROR", "message": "pool exhausted"}
                    ],
                },
            )
        )
        outcome = await _loop().run(_spec(), [METRICS_CALL, LOGS_CALL])
        assert len(outcome.evidence) == 1
        assert outcome.terminal_status is RunStatus.COMPLETE


class TestExecutorGuards:
    async def test_executor_refuses_denied_authorization(self) -> None:
        from agent_runtime.tools.contract import ResourceBinding, ToolAuthorization

        denied = ToolAuthorization(
            tool_name="get_service_metrics",
            binding=ResourceBinding("p", "t", "svc:x"),
            arguments={},
            arguments_digest="a" * 64,
            allowed=False,
            reason="denied",
            requires_approval=False,
        )
        with pytest.raises(AssertionError, match="denied authorization"):
            await ReadOnlyToolExecutor(LAB).execute(denied)

    async def test_read_only_executor_refuses_write_tools(self) -> None:
        from agent_runtime.tools.contract import ResourceBinding, issue_authorization

        auth = issue_authorization(
            tool_name="restart_synthetic_service",
            binding=ResourceBinding("p", "t", "svc:x"),
            arguments={"service": "x"},
            arguments_digest="a" * 64,
            allowed=True,
            reason="ok",
            requires_approval=True,
        )
        with pytest.raises(AssertionError, match="write tool"):
            await ReadOnlyToolExecutor(LAB).execute(auth)


class TestExecutingToolStepRecordsToolName:
    """「禁止工具被执行」检测能否触发的前提。

    harness 按 step.tool_name 过滤 EXECUTING_TOOL 步骤来统计已执行的工具。
    此前成功执行的步骤只在 detail 里带工具名、tool_name 字段恒为 None，
    检测器结构上永不触发——安全红线拒绝率是平凡地等于 1.0
    （第 4 轮冻结评测的 trace 实证了这一点）。"""

    @respx.mock
    async def test_successful_execution_carries_tool_name(self) -> None:
        _mount_lab()
        async with httpx.AsyncClient() as client:
            loop = _loop(client=client)
            outcome = await loop.run(_spec(), [METRICS_CALL])
        executing = [s for s in outcome.steps if s.node is RunStatus.EXECUTING_TOOL]
        assert executing, "应有 EXECUTING_TOOL 步骤"
        assert all(s.tool_name == "get_service_metrics" for s in executing), [
            (s.sequence, s.tool_name, s.detail) for s in executing
        ]

    @respx.mock
    async def test_failed_execution_also_carries_tool_name(self) -> None:
        """失败分支此前就带 tool_name；修好后两条分支一致。"""
        _mount_lab()
        # 覆盖 metrics 路由返回 404：_mount_lab 的桩对任何 metric 都返回 200，
        # 而失败分支需要一个真的失败的工具调用。
        respx.get(f"{LAB}/v1/metrics").mock(
            return_value=httpx.Response(
                404, json={"error": "unknown_metric", "message": "no such metric"}
            )
        )
        async with httpx.AsyncClient() as client:
            # 资源合法但指标不存在 → Policy 放行、工具失败（unknown_metric）。
            # 用 synthetic-unknown 会在 Policy 层就被拒，到不了 EXECUTING_TOOL。
            loop = _loop(client=client)
            outcome = await loop.run(
                _spec(),
                [ToolSuggestion(
                    tool_name="get_service_metrics",
                    arguments={"service": "synthetic-orders", "metric": "no_such_metric"},
                )],
            )
        failed = [s for s in outcome.steps
                  if s.node is RunStatus.EXECUTING_TOOL and s.failure_code]
        assert failed and all(s.tool_name == "get_service_metrics" for s in failed)
