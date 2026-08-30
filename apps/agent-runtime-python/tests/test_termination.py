"""八条终止条件与状态机的测试。

M0 §6 要求「每一条都要有对应单元测试」。这里逐条构造触发场景，并断言
failure_class 与 contracts/events/README.md 的枚举一致——M6 的失败归因统计
依赖这个映射。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_runtime.agent.state import (
    IllegalTransitionError,
    RunStatus,
    can_transition,
    require_transition,
)
from agent_runtime.agent.termination import (
    BoundedLoopGuard,
    BudgetState,
    RepeatedStateDetector,
    TerminationReason,
    state_fingerprint,
)

T0 = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _budget(**overrides) -> BudgetState:
    defaults = dict(
        max_steps=25,
        deadline=T0 + timedelta(minutes=10),
        cost_budget_micros=500_000,
        token_budget=200_000,
        tool_call_budget=20,
    )
    defaults.update(overrides)
    return BudgetState(**defaults)


def _guard(budget: BudgetState | None = None, *, now: datetime = T0) -> BoundedLoopGuard:
    return BoundedLoopGuard(budget or _budget(), now=lambda: now)


class TestBudgetConstruction:
    @pytest.mark.parametrize(
        "field", ["max_steps", "cost_budget_micros", "token_budget", "tool_call_budget"]
    )
    def test_non_positive_budget_is_rejected(self, field: str) -> None:
        """无上限的 Run 是被禁止的（NG-6）。0 或负数在构造期就挡住。"""
        with pytest.raises(ValueError, match="unbounded runs are forbidden"):
            _budget(**{field: 0})


class TestEightTerminationConditions:
    """M0 §6 的八条，逐条一个测试。"""

    def test_1_max_steps_exhausted(self) -> None:
        guard = _guard(_budget(max_steps=3))
        guard.budget.steps_used = 3
        verdict = guard.check()
        assert verdict.should_stop
        assert verdict.reason is TerminationReason.MAX_STEPS_EXHAUSTED

    def test_2_deadline_exceeded(self) -> None:
        guard = _guard(_budget(deadline=T0 - timedelta(seconds=1)))
        verdict = guard.check()
        assert verdict.reason is TerminationReason.DEADLINE_EXCEEDED

    def test_3_cost_budget_exhausted(self) -> None:
        guard = _guard(_budget(cost_budget_micros=1000))
        guard.charge_model(tokens=0, cost_micros=1000)
        assert guard.check().reason is TerminationReason.COST_BUDGET_EXHAUSTED

    def test_3b_token_budget_exhausted(self) -> None:
        guard = _guard(_budget(token_budget=500))
        guard.charge_model(tokens=500, cost_micros=0)
        assert guard.check().reason is TerminationReason.TOKEN_BUDGET_EXHAUSTED

    def test_4_tool_call_budget_exhausted(self) -> None:
        guard = _guard(_budget(tool_call_budget=2))
        guard.charge_tool_call()
        guard.charge_tool_call()
        assert guard.check().reason is TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED

    def test_5_repeated_state_detected(self) -> None:
        guard = _guard()
        fp = state_fingerprint("SELECT_TOOL", ["ev-1"], "get_service_metrics")
        for _ in range(3):
            guard.repeated_state.observe(fp)
        assert guard.check(fingerprint=fp).reason is TerminationReason.REPEATED_STATE_DETECTED

    def test_6_authorization_failed(self) -> None:
        assert _guard().check(authorization_failed=True).reason is (
            TerminationReason.AUTHORIZATION_FAILED
        )

    def test_7_version_incompatible(self) -> None:
        assert _guard().check(version_incompatible=True).reason is (
            TerminationReason.VERSION_INCOMPATIBLE
        )

    def test_8_safety_rule_triggered(self) -> None:
        assert _guard().check(safety_rule_hit=True).reason is (
            TerminationReason.SAFETY_RULE_TRIGGERED
        )

    def test_no_condition_hit_allows_continue(self) -> None:
        assert _guard().check().should_stop is False


class TestTerminationPrecedence:
    def test_budget_wins_over_loop(self) -> None:
        """既超预算又循环时归因到预算——预算是更根本的约束，
        而「为什么停了」在 M6 里必须有唯一答案。"""
        guard = _guard(_budget(max_steps=1))
        guard.budget.steps_used = 1
        fp = state_fingerprint("SELECT_TOOL", [], "x")
        for _ in range(5):
            guard.repeated_state.observe(fp)
        assert guard.check(fingerprint=fp).reason is TerminationReason.MAX_STEPS_EXHAUSTED

    def test_steps_win_over_deadline(self) -> None:
        guard = _guard(_budget(max_steps=1, deadline=T0 - timedelta(hours=1)))
        guard.budget.steps_used = 1
        assert guard.check().reason is TerminationReason.MAX_STEPS_EXHAUSTED


class TestFailureClassMapping:
    def test_every_reason_matches_the_event_contract_enum(self) -> None:
        """failureClass 必须与 contracts/events/README.md 的枚举一致。
        两侧漂移会让 Control Plane 拒收 Worker 上报的终态。"""
        contract_enum = {
            "max_steps_exhausted",
            "deadline_exceeded",
            "cost_budget_exhausted",
            "token_budget_exhausted",
            "tool_call_budget_exhausted",
            "repeated_state_detected",
            "authorization_failed",
            "version_incompatible",
            "safety_rule_triggered",
            "insufficient_evidence",
            "provider_failure",
            "tool_failure",
            "cancelled",
            "max_delivery_exceeded",
            "internal_error",
        }
        for reason in TerminationReason:
            assert reason.value in contract_enum, f"{reason.value} not in event contract"


class TestRepeatedStateDetector:
    def test_fingerprint_ignores_ordering_of_evidence(self) -> None:
        a = state_fingerprint("SELECT_TOOL", ["ev-2", "ev-1"], "t")
        b = state_fingerprint("SELECT_TOOL", ["ev-1", "ev-2"], "t")
        assert a == b

    def test_fingerprint_distinguishes_pending_tool(self) -> None:
        a = state_fingerprint("SELECT_TOOL", ["ev-1"], "get_service_metrics")
        b = state_fingerprint("SELECT_TOOL", ["ev-1"], "search_service_logs")
        assert a != b

    def test_new_evidence_breaks_the_loop(self) -> None:
        """取到新证据说明处境变了，不该判定为循环。"""
        detector = RepeatedStateDetector(limit=2)
        first = state_fingerprint("SELECT_TOOL", [], "t")
        detector.observe(first)
        detector.observe(first)
        assert detector.is_looping(first)
        second = state_fingerprint("SELECT_TOOL", ["ev-1"], "t")
        assert not detector.is_looping(second)

    def test_limit_is_inclusive(self) -> None:
        detector = RepeatedStateDetector(limit=3)
        fp = "x"
        detector.observe(fp)
        detector.observe(fp)
        assert not detector.is_looping(fp)
        detector.observe(fp)
        assert detector.is_looping(fp)


class TestStateMachine:
    def test_terminal_states(self) -> None:
        assert RunStatus.COMPLETE.is_terminal()
        assert RunStatus.FAILED.is_terminal()
        assert RunStatus.BLOCKED.is_terminal()
        assert not RunStatus.VERIFY.is_terminal()

    def test_happy_path_transitions(self) -> None:
        path = [
            RunStatus.CREATED,
            RunStatus.COLLECT_CONTEXT,
            RunStatus.RETRIEVE_RUNBOOK,
            RunStatus.FORM_HYPOTHESES,
            RunStatus.SELECT_TOOL,
            RunStatus.POLICY_CHECK,
            RunStatus.EXECUTING_TOOL,
            RunStatus.OBSERVE,
            RunStatus.VERIFY,
            RunStatus.PROPOSE_ACTION,
        ]
        for current, target in zip(path, path[1:]):
            require_transition(current, target)

    def test_approval_branch(self) -> None:
        require_transition(RunStatus.POLICY_CHECK, RunStatus.AWAITING_APPROVAL)
        require_transition(RunStatus.AWAITING_APPROVAL, RunStatus.EXECUTING_TOOL)

    def test_need_more_evidence_loops_back(self) -> None:
        """NEED_MORE_EVIDENCE 是一条边而非一个状态：做成状态会多一个没有实际工作的节点。"""
        require_transition(RunStatus.VERIFY, RunStatus.SELECT_TOOL)

    def test_any_state_can_go_terminal(self) -> None:
        """终止条件可以在任何节点命中。"""
        for status in RunStatus:
            if status.is_terminal():
                continue
            assert can_transition(status, RunStatus.FAILED)
            assert can_transition(status, RunStatus.BLOCKED)

    def test_terminal_state_accepts_no_transition(self) -> None:
        """INV-5：唯一终态。终态之后不接受任何转移，包括转到另一个终态。"""
        for terminal in (RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.BLOCKED):
            for target in RunStatus:
                assert not can_transition(terminal, target)

    def test_skipping_a_stage_is_rejected(self) -> None:
        with pytest.raises(IllegalTransitionError):
            require_transition(RunStatus.CREATED, RunStatus.EXECUTING_TOOL)

    def test_executing_without_policy_check_is_rejected(self) -> None:
        """三段分离在状态机层面的体现：不经 POLICY_CHECK 到不了 EXECUTING_TOOL。"""
        with pytest.raises(IllegalTransitionError):
            require_transition(RunStatus.SELECT_TOOL, RunStatus.EXECUTING_TOOL)

    def test_awaiting_approval_cannot_be_skipped_backwards(self) -> None:
        with pytest.raises(IllegalTransitionError):
            require_transition(RunStatus.AWAITING_APPROVAL, RunStatus.SELECT_TOOL)
