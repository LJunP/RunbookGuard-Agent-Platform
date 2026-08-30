"""八条硬性终止条件（M0 §6）。

每条都映射到 contracts/events/README.md 的一个 failureClass。守卫是纯函数式的：
所有事实由 BudgetState 与注入的时钟提供，因此可以在测试里精确控制每一条的触发。

**不用 LangGraph 的 recursion_limit**：它只能表达「最多走多少步」，触发时抛的是框架
异常，说不出是 max_steps 还是 deadline。而 M6 的失败归因统计需要区分这八种。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable


class TerminationReason(str, Enum):
    """取值与 failureClass 一一对应，顺序即 M0 §6 的八条。"""

    MAX_STEPS_EXHAUSTED = "max_steps_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    COST_BUDGET_EXHAUSTED = "cost_budget_exhausted"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    TOOL_CALL_BUDGET_EXHAUSTED = "tool_call_budget_exhausted"
    REPEATED_STATE_DETECTED = "repeated_state_detected"
    AUTHORIZATION_FAILED = "authorization_failed"
    VERSION_INCOMPATIBLE = "version_incompatible"
    SAFETY_RULE_TRIGGERED = "safety_rule_triggered"


@dataclass
class BudgetState:
    """有界执行的全部预算。字段与 M1 的 agent_run 表一一对应。"""

    max_steps: int
    deadline: datetime
    cost_budget_micros: int
    token_budget: int
    tool_call_budget: int
    steps_used: int = 0
    cost_spent_micros: int = 0
    token_spent: int = 0
    tool_calls_used: int = 0

    def __post_init__(self) -> None:
        for name in ("max_steps", "cost_budget_micros", "token_budget", "tool_call_budget"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive; unbounded runs are forbidden")


@dataclass
class RepeatedStateDetector:
    """防死循环（M0 终止条件 5）。

    用「状态指纹的出现次数」而非相似度阈值：相似度需要一个阈值，而阈值是需要调的
    参数，会让「为什么这次判定循环了」变得难以解释。指纹相同意味着 Agent 回到了
    完全一样的处境，继续下去只会得到一样的结果。

    已知局限：语义等价但表述不同的状态不会被识别（M0 威胁模型 T-5 的残余风险）。
    """

    limit: int = 3
    seen: dict[str, int] = field(default_factory=dict)

    def observe(self, fingerprint: str) -> int:
        count = self.seen.get(fingerprint, 0) + 1
        self.seen[fingerprint] = count
        return count

    def is_looping(self, fingerprint: str) -> bool:
        return self.seen.get(fingerprint, 0) >= self.limit


def state_fingerprint(node: str, evidence_ids: list[str], pending_tool: str | None) -> str:
    """状态指纹。

    刻意只取 (节点, 已有证据集合, 待调用工具)：这三者相同就意味着下一步的输入完全一样。
    把时间戳或步数纳入指纹会让它永不重复，检测直接失效。
    """
    payload = json.dumps(
        {"node": node, "evidence": sorted(evidence_ids), "pending_tool": pending_tool},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()


@dataclass(frozen=True)
class TerminationVerdict:
    should_stop: bool
    reason: TerminationReason | None = None
    detail: str = ""


class BoundedLoopGuard:
    """八条终止条件的唯一判定点。

    求值顺序是刻意的：先判定「已经用超了」的硬上限，再判定循环，最后是外部注入的
    安全与版本条件。这样一个既超预算又循环的 Run 会被归因到预算，
    因为预算是更根本的约束。
    """

    def __init__(
        self,
        budget: BudgetState,
        *,
        now: Callable[[], datetime],
        repeated_state: RepeatedStateDetector | None = None,
    ) -> None:
        self.budget = budget
        self._now = now
        self.repeated_state = repeated_state or RepeatedStateDetector()

    def check(
        self,
        *,
        fingerprint: str | None = None,
        authorization_failed: bool = False,
        version_incompatible: bool = False,
        safety_rule_hit: bool = False,
    ) -> TerminationVerdict:
        b = self.budget

        if b.steps_used >= b.max_steps:
            return TerminationVerdict(
                True,
                TerminationReason.MAX_STEPS_EXHAUSTED,
                f"steps {b.steps_used}/{b.max_steps}",
            )

        now = self._now()
        if now >= b.deadline:
            return TerminationVerdict(
                True,
                TerminationReason.DEADLINE_EXCEEDED,
                f"deadline {b.deadline.isoformat()} reached at {now.isoformat()}",
            )

        if b.cost_spent_micros >= b.cost_budget_micros:
            return TerminationVerdict(
                True,
                TerminationReason.COST_BUDGET_EXHAUSTED,
                f"cost {b.cost_spent_micros}/{b.cost_budget_micros} micros",
            )

        if b.token_spent >= b.token_budget:
            return TerminationVerdict(
                True,
                TerminationReason.TOKEN_BUDGET_EXHAUSTED,
                f"tokens {b.token_spent}/{b.token_budget}",
            )

        if b.tool_calls_used >= b.tool_call_budget:
            return TerminationVerdict(
                True,
                TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED,
                f"tool calls {b.tool_calls_used}/{b.tool_call_budget}",
            )

        if fingerprint is not None and self.repeated_state.is_looping(fingerprint):
            return TerminationVerdict(
                True,
                TerminationReason.REPEATED_STATE_DETECTED,
                f"state {fingerprint[:12]} seen {self.repeated_state.seen[fingerprint]} times",
            )

        # 下面三条由调用方注入：它们不是「用超了」，而是外部判定的结果。
        if authorization_failed:
            return TerminationVerdict(
                True, TerminationReason.AUTHORIZATION_FAILED, "authorization check failed"
            )
        if version_incompatible:
            return TerminationVerdict(
                True,
                TerminationReason.VERSION_INCOMPATIBLE,
                "graph or state schema version is incompatible",
            )
        if safety_rule_hit:
            return TerminationVerdict(
                True, TerminationReason.SAFETY_RULE_TRIGGERED, "a safety rule was triggered"
            )

        return TerminationVerdict(False)

    def charge_step(self) -> None:
        self.budget.steps_used += 1

    def charge_tool_call(self) -> None:
        self.budget.tool_calls_used += 1

    def charge_model(self, *, tokens: int, cost_micros: int) -> None:
        self.budget.token_spent += tokens
        self.budget.cost_spent_micros += cost_micros
