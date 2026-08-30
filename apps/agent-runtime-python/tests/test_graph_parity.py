"""两条执行路径的一致性测试（ADR-0006 §2）。

这些测试让「LangGraph 替我做了什么」有一个可执行的答案：
  - 行为相同的部分 = 我自己实现的（状态机、8 条终止条件、Policy、三段分离）
  - 只有 LangGraph 那条具备的 = 框架的贡献（interrupt/resume、检查点跨进程恢复）

如果哪天这些一致性测试开始失败，说明两条路径的行为分叉了——那时必须先解释清楚
是哪一条错了，而不是把测试改成接受差异。
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
from agent_runtime.agent.graph import AgentGraph
from agent_runtime.agent.state import RunStatus
from agent_runtime.agent.termination import BoundedLoopGuard, BudgetState
from agent_runtime.approval.digest import digest as digest_fn
from agent_runtime.provider.fake import FakeProvider, FakeTurn
from agent_runtime.tools import catalogue
from agent_runtime.tools.contract import ToolSuggestion
from agent_runtime.tools.action_executor import ActionToolExecutor
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
FORBIDDEN_CALL = ToolSuggestion(
    tool_name="restart_synthetic_service",
    arguments={"service": "synthetic-db"},
)


@dataclass
class StubApprovalGateway:
    decision: str = "PENDING"
    consumed: bool = False
    approved_arguments: dict[str, Any] | None = None
    consume_calls: list[dict[str, Any]] = field(default_factory=list)
    _tool_name: str = ""
    _resource_ref: str = ""

    async def request(self, *, run_id, tool_name, resource_ref, arguments) -> str:
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
        run_id="run-parity-1",
        tenant_id="tenant-demo",
        principal_id="prin-agent",
        incident_summary="synthetic-orders p99 above 3s",
        allowed_tool_names=frozenset(t.name for t in catalogue.READ_ONLY_TOOLS),
        permitted_resources=frozenset({"svc:synthetic-orders", "queue:synthetic-notify.work"}),
    )
    defaults.update(overrides)
    return RunSpec(**defaults)


def _mount_lab() -> None:
    respx.get(f"{LAB}/v1/metrics").mock(
        return_value=httpx.Response(
            200,
            json={
                "service": "synthetic-orders",
                "metric": "db_pool_active",
                "unit": "connections",
                "coverage": "covered",
                "points": [{"timestamp": "2026-08-27T11:30:00+00:00", "value": 12.0}],
            },
        )
    )
    respx.get(f"{LAB}/v1/runtime-events").mock(
        return_value=httpx.Response(
            200, json={"service": "synthetic-orders", "coverage": "covered", "events": []}
        )
    )
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


def _mount_actions() -> None:
    """动作端点。只在需要真正执行动作的测试里挂载——默认不挂，
    这样任何意料之外的动作调用都会以 respx 断言失败的形式暴露，而不是静默成功。"""
    respx.post(f"{LAB}/v1/actions/rollback").mock(
        return_value=httpx.Response(
            200,
            json={
                "idempotency_key": "k",
                "action": "rollback",
                "service": "synthetic-orders",
                "detail": {"from": "v1.5.0", "to": "v1.4.2"},
                "performed_at": "2026-08-27T12:05:00+00:00",
                "replayed": False,
            },
        )
    )


async def _run_loop(plan, *, spec=None, budget=None, approvals=None, provider=None, **flags):
    loop = BoundedAgentLoop(
        provider=provider or FakeProvider(script=[FakeTurn(text=VALID_DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB),
        guard=BoundedLoopGuard(budget or _budget(), now=lambda: T0),
        approvals=approvals,
        **flags,
    )
    return await loop.run(spec or _spec(), plan)


async def _run_graph(plan, *, spec=None, budget=None, approvals=None, provider=None,
                     action_executor=None, checkpointer=None, **flags):
    graph = AgentGraph(
        provider=provider or FakeProvider(script=[FakeTurn(text=VALID_DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB),
        # sandbox=False：这些测试用 respx 打桩 HTTP，而子进程有自己的
        # 事件循环与网络栈，respx 的 mock 到不了那里。沙箱本身由
        # test_action_sandbox.py 对着真实子进程验证。
        action_executor=action_executor or ActionToolExecutor(LAB, sandbox=False),
        guard=BoundedLoopGuard(budget or _budget(), now=lambda: T0),
        spec=spec or _spec(),
        approvals=approvals,
        checkpointer=checkpointer,
        **flags,
    )
    return graph, await graph.run(plan)


class TestParityOnHappyPath:
    @respx.mock
    async def test_both_reach_complete_with_same_evidence(self) -> None:
        _mount_lab()
        loop_outcome = await _run_loop([METRICS_CALL, LOGS_CALL])
        _, graph_outcome = await _run_graph([METRICS_CALL, LOGS_CALL])

        assert loop_outcome.terminal_status is graph_outcome.terminal_status is RunStatus.COMPLETE
        assert loop_outcome.failure_class == graph_outcome.failure_class is None
        assert {e.evidence_id for e in loop_outcome.evidence} == {
            e["evidence_id"] for e in graph_outcome.evidence
        }

    @respx.mock
    async def test_both_produce_the_same_diagnosis(self) -> None:
        _mount_lab()
        loop_outcome = await _run_loop([METRICS_CALL])
        _, graph_outcome = await _run_graph([METRICS_CALL])
        assert loop_outcome.diagnosis["root_cause"] == graph_outcome.diagnosis["root_cause"]
        assert loop_outcome.diagnosis["conclusion_type"] == graph_outcome.diagnosis["conclusion_type"]

    @respx.mock
    async def test_both_visit_policy_check_before_executing(self) -> None:
        """三段分离在两条路径上都成立。"""
        _mount_lab()
        loop_outcome = await _run_loop([METRICS_CALL])
        _, graph_outcome = await _run_graph([METRICS_CALL])

        loop_nodes = [s.node.value for s in loop_outcome.steps]
        graph_nodes = [s["node"] for s in graph_outcome.steps]
        for nodes in (loop_nodes, graph_nodes):
            assert nodes.index("POLICY_CHECK") < nodes.index("EXECUTING_TOOL")


class TestParityOnTermination:
    """八条终止条件在两条路径上必须给出同一个 failure_class。"""

    @respx.mock
    async def test_tool_call_budget(self) -> None:
        _mount_lab()
        loop_outcome = await _run_loop([METRICS_CALL, LOGS_CALL], budget=_budget(tool_call_budget=1))
        _, graph_outcome = await _run_graph([METRICS_CALL, LOGS_CALL], budget=_budget(tool_call_budget=1))
        assert loop_outcome.failure_class == graph_outcome.failure_class == "tool_call_budget_exhausted"

    @respx.mock
    async def test_deadline(self) -> None:
        _mount_lab()
        past = _budget(deadline=T0 - timedelta(seconds=1))
        loop_outcome = await _run_loop([METRICS_CALL], budget=past)
        _, graph_outcome = await _run_graph([METRICS_CALL], budget=_budget(deadline=T0 - timedelta(seconds=1)))
        assert loop_outcome.failure_class == graph_outcome.failure_class == "deadline_exceeded"

    @respx.mock
    async def test_max_steps(self) -> None:
        _mount_lab()
        loop_outcome = await _run_loop([METRICS_CALL], budget=_budget(max_steps=2))
        _, graph_outcome = await _run_graph([METRICS_CALL], budget=_budget(max_steps=2))
        assert loop_outcome.failure_class == graph_outcome.failure_class == "max_steps_exhausted"

    @respx.mock
    async def test_version_incompatible(self) -> None:
        _mount_lab()
        loop_outcome = await _run_loop([METRICS_CALL], version_incompatible=True)
        _, graph_outcome = await _run_graph([METRICS_CALL], version_incompatible=True)
        assert loop_outcome.failure_class == graph_outcome.failure_class == "version_incompatible"

    @respx.mock
    async def test_safety_rule(self) -> None:
        _mount_lab()
        loop_outcome = await _run_loop([METRICS_CALL], safety_rule_hit=True)
        _, graph_outcome = await _run_graph([METRICS_CALL], safety_rule_hit=True)
        assert loop_outcome.failure_class == graph_outcome.failure_class == "safety_rule_triggered"

    @respx.mock
    async def test_model_garbage(self) -> None:
        _mount_lab()
        loop_outcome = await _run_loop(
            [METRICS_CALL], provider=FakeProvider(script=[FakeTurn(text="not json")])
        )
        _, graph_outcome = await _run_graph(
            [METRICS_CALL], provider=FakeProvider(script=[FakeTurn(text="not json")])
        )
        assert loop_outcome.failure_class == graph_outcome.failure_class == "provider_failure"


class TestParityOnSecurity:
    @respx.mock
    async def test_forbidden_tool_denied_on_both(self) -> None:
        """allowlist 外的工具在两条路径上都拒绝，且都不终止整个 Run。"""
        _mount_lab()
        spec = _spec(allowed_tool_names=frozenset({"get_service_metrics"}))
        loop_outcome = await _run_loop([FORBIDDEN_CALL, METRICS_CALL], spec=spec)
        _, graph_outcome = await _run_graph([FORBIDDEN_CALL, METRICS_CALL], spec=spec)

        loop_codes = {s.failure_code for s in loop_outcome.steps}
        graph_codes = {s["failure_code"] for s in graph_outcome.steps}
        assert "not_in_allowlist" in loop_codes
        assert "not_in_allowlist" in graph_codes
        # 被拒绝之后仍用其它工具取到了证据
        assert loop_outcome.evidence and graph_outcome.evidence

    @respx.mock
    async def test_write_without_approval_denied_on_both(self) -> None:
        _mount_lab()
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))
        loop_outcome = await _run_loop([ROLLBACK_CALL], spec=spec, approvals=None)
        _, graph_outcome = await _run_graph([ROLLBACK_CALL], spec=spec, approvals=None)

        loop_codes = {s.failure_code for s in loop_outcome.steps}
        graph_codes = {s["failure_code"] for s in graph_outcome.steps}
        assert "approval_required_but_absent" in loop_codes
        assert "approval_required_but_absent" in graph_codes
        assert not loop_outcome.evidence
        assert not graph_outcome.evidence

    @respx.mock
    async def test_pending_approval_suspends_on_both(self) -> None:
        _mount_lab()
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))
        loop_gateway = StubApprovalGateway(decision="PENDING")
        graph_gateway = StubApprovalGateway(decision="PENDING")
        loop_outcome = await _run_loop([ROLLBACK_CALL], spec=spec, approvals=loop_gateway)
        _, graph_outcome = await _run_graph([ROLLBACK_CALL], spec=spec, approvals=graph_gateway)

        assert loop_outcome.terminal_status is RunStatus.AWAITING_APPROVAL
        assert graph_outcome.terminal_status is RunStatus.AWAITING_APPROVAL
        assert loop_gateway.consume_calls == []
        assert graph_gateway.consume_calls == []

    @respx.mock
    async def test_is_decoy_stripped_on_both(self) -> None:
        _mount_lab()
        spec = _spec(allowed_tool_names=frozenset({"get_recent_deployments"}))
        call = ToolSuggestion(
            tool_name="get_recent_deployments", arguments={"service": "synthetic-orders"}
        )
        loop_outcome = await _run_loop([call], spec=spec)
        _, graph_outcome = await _run_graph([call], spec=spec)
        # 两条路径的证据摘要相同即证明剥除行为一致——摘要是对 payload 算的。
        assert {e.content_hash for e in loop_outcome.evidence} == {
            e["content_hash"] for e in graph_outcome.evidence
        }


class TestWhatLangGraphAdds:
    """只有 LangGraph 那条具备的能力。这些测试标出框架的实际贡献边界。"""

    @respx.mock
    async def test_interrupt_leaves_the_graph_resumable(self) -> None:
        """手写循环在挂起时只能返回一个结果对象；图则把状态交给 checkpointer，
        可以用 Command(resume=...) 从中断处继续。"""
        _mount_lab()
        _mount_actions()
        gateway = StubApprovalGateway(decision="PENDING")
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))
        graph, outcome = await _run_graph([ROLLBACK_CALL], spec=spec, approvals=gateway)

        assert outcome.interrupted is True
        assert outcome.awaiting_approval_id == "apr-stub-1"

        # 外部事件：人批准了。
        gateway.decision = "APPROVED"
        resumed = await graph.resume(
            decision={"decision": "APPROVED", "approval_id": "apr-stub-1"},
            thread_id=spec.run_id,
        )
        assert gateway.consume_calls, "恢复后必须调 Java 侧的 consume 做四项比对"
        assert resumed.terminal_status in {RunStatus.COMPLETE, RunStatus.FAILED}

    @respx.mock
    async def test_resume_payload_is_not_an_authorisation(self) -> None:
        """M0 INV-4：resume 里说 APPROVED 不代表能执行。

        构造方式：resume 声称已批准，但 Java 侧的审批实际仍是 PENDING。
        执行必须被拒绝。
        """
        _mount_lab()
        gateway = StubApprovalGateway(decision="PENDING")
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))
        graph, outcome = await _run_graph([ROLLBACK_CALL], spec=spec, approvals=gateway)
        assert outcome.interrupted is True

        # 谎称已批准，但不改 gateway 的真实状态。
        resumed = await graph.resume(
            decision={"decision": "APPROVED", "approval_id": "apr-stub-1"},
            thread_id=spec.run_id,
        )
        assert resumed.terminal_status is RunStatus.FAILED
        assert resumed.failure_class == "authorization_failed"
        assert gateway.consumed is False, "审批未被消费，说明动作没有执行"

    @respx.mock
    async def test_checkpoint_state_survives_a_new_graph_object(self) -> None:
        """跨进程恢复的最小证明：用同一个 checkpointer 构造一个新的 AgentGraph
        实例（模拟新进程），仍能从中断处恢复。"""
        from langgraph.checkpoint.memory import InMemorySaver

        _mount_lab()
        _mount_actions()
        shared = InMemorySaver()
        gateway = StubApprovalGateway(decision="PENDING")
        spec = _spec(allowed_tool_names=frozenset({"rollback_synthetic_deployment"}))

        first = AgentGraph(
            provider=FakeProvider(script=[FakeTurn(text=VALID_DIAGNOSIS)]),
            policy=PolicyEngine(),
            executor=ReadOnlyToolExecutor(LAB),
            guard=BoundedLoopGuard(_budget(), now=lambda: T0),
            spec=spec,
            approvals=gateway,
            checkpointer=shared,
        )
        outcome = await first.run([ROLLBACK_CALL])
        assert outcome.interrupted is True

        gateway.decision = "APPROVED"
        second = AgentGraph(
            provider=FakeProvider(script=[FakeTurn(text=VALID_DIAGNOSIS)]),
            policy=PolicyEngine(),
            executor=ReadOnlyToolExecutor(LAB),
            guard=BoundedLoopGuard(_budget(), now=lambda: T0),
            spec=spec,
            approvals=gateway,
            checkpointer=shared,
        )
        # 不需要手工回填任何东西：待执行的建议在 checkpoint 的 current 通道里。
        # 这正是「把活对象放 self」与「放状态」的差别——前者跨进程恢复不了。
        resumed = await second.resume(
            decision={"decision": "APPROVED"}, thread_id=spec.run_id
        )
        assert gateway.consume_calls, "新实例恢复后仍要走 Java 侧的 consume"
        assert resumed.terminal_status in {RunStatus.COMPLETE, RunStatus.FAILED}


class TestGraphVersioning:
    def test_graph_declares_its_versions(self) -> None:
        """恢复前要校验 graph_version 与 state_schema_version 兼容
        （M0 终止条件 7）。版本必须是显式常量，不能靠推断。"""
        assert AgentGraph.GRAPH_VERSION == "langgraph-v1"
        assert AgentGraph.STATE_SCHEMA_VERSION == "1"
