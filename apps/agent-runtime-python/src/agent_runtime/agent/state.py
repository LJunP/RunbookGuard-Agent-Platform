"""Agent 状态机（M0 §6）。

状态与转移的权威定义在这里，不在 LangGraph 里（ADR-0006 §1）。理由：这些是业务事实，
换编排框架不该重写它们；而且 8 条终止条件各自要映射到一个 failureClass，
框架的 recursion_limit 说不出「是 max_steps 还是 deadline」。
"""

from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    CREATED = "CREATED"
    COLLECT_CONTEXT = "COLLECT_CONTEXT"
    RETRIEVE_RUNBOOK = "RETRIEVE_RUNBOOK"
    FORM_HYPOTHESES = "FORM_HYPOTHESES"
    SELECT_TOOL = "SELECT_TOOL"
    POLICY_CHECK = "POLICY_CHECK"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING_TOOL = "EXECUTING_TOOL"
    OBSERVE = "OBSERVE"
    VERIFY = "VERIFY"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    BLOCKED = "BLOCKED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"

    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL: frozenset[RunStatus] = frozenset(
    {RunStatus.BLOCKED, RunStatus.COMPLETE, RunStatus.FAILED}
)

# 合法转移。任何状态都能进终态（终止条件可以在任何节点命中），因此终态不在这张表里列举，
# 由 can_transition 单独放行。
_ALLOWED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.COLLECT_CONTEXT}),
    RunStatus.COLLECT_CONTEXT: frozenset({RunStatus.RETRIEVE_RUNBOOK}),
    RunStatus.RETRIEVE_RUNBOOK: frozenset({RunStatus.FORM_HYPOTHESES}),
    RunStatus.SELECT_TOOL: frozenset({RunStatus.POLICY_CHECK}),
    # POLICY_CHECK 拒绝一次工具建议之后可以再选下一个工具，也可以直接去结论
    # ——拒绝不终止整个 Run（S6 的注入 case 依赖这一点：动作被拦住但诊断继续）。
    RunStatus.POLICY_CHECK: frozenset(
        {
            RunStatus.AWAITING_APPROVAL,
            RunStatus.EXECUTING_TOOL,
            RunStatus.SELECT_TOOL,
            RunStatus.VERIFY,
            RunStatus.PROPOSE_ACTION,
        }
    ),
    RunStatus.AWAITING_APPROVAL: frozenset({RunStatus.EXECUTING_TOOL}),
    RunStatus.EXECUTING_TOOL: frozenset({RunStatus.OBSERVE}),
    RunStatus.OBSERVE: frozenset({RunStatus.VERIFY}),
    # NEED_MORE_EVIDENCE 不是一个状态，而是 VERIFY 回到 SELECT_TOOL 这条边。
    # 把它做成状态会多一个没有实际工作的节点。
    RunStatus.VERIFY: frozenset({RunStatus.SELECT_TOOL, RunStatus.PROPOSE_ACTION}),
    # 无工具可用时（allowlist 为空、或计划为空）直接从 FORM_HYPOTHESES 去结论。
    RunStatus.FORM_HYPOTHESES: frozenset(
        {RunStatus.SELECT_TOOL, RunStatus.VERIFY, RunStatus.PROPOSE_ACTION}
    ),
    RunStatus.PROPOSE_ACTION: frozenset({RunStatus.SELECT_TOOL}),
    RunStatus.BLOCKED: frozenset(),
    RunStatus.COMPLETE: frozenset(),
    RunStatus.FAILED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        super().__init__(f"illegal transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    if current.is_terminal():
        # 终态之后不接受任何转移，包括转到另一个终态（INV-5：唯一终态）。
        return False
    if target.is_terminal():
        return True
    return target in _ALLOWED[current]


def require_transition(current: RunStatus, target: RunStatus) -> None:
    if not can_transition(current, target):
        raise IllegalTransitionError(current, target)
