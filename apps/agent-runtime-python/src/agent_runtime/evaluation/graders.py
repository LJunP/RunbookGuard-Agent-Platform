"""结构化 grader（ADR-0009）。

把语义判据转成确定性判定：字符串比对、集合运算、hash 反查。
同一份产出永远得到同一个判定——「跑一次」这个纪律因此有意义。

不用 LLM-as-judge 的三个理由见 ADR-0009 §2。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agent.bounded_loop import Evidence
from ..schemas import ConclusionType, Diagnosis


@dataclass(frozen=True)
class GroundednessResult:
    total_claims: int
    grounded_claims: int
    unsupported: tuple[str, ...]

    @property
    def score(self) -> float:
        """没有 claim 时返回 0.0 而不是 1.0。

        一个声称给出了诊断却不列任何断言的结论，不是「没有无支撑断言」，
        是「无法验证它说了什么」。空 claims 只在弃答结论上是合理的，
        那种情况由 conclusion_type 单独判定。
        """
        return self.grounded_claims / self.total_claims if self.total_claims else 0.0


def check_groundedness(
    diagnosis: Diagnosis, evidence: list[Evidence]
) -> GroundednessResult:
    """每个事实断言必须引用本 Run 实际收集到的证据。

    校验的是**集合归属**而非格式：模型可以编造一个格式正确的 ev-abc123，
    但它不在集合里（ADR-0009 §3 第 2 条）。
    """
    available = {e.evidence_id for e in evidence}
    unsupported: list[str] = []
    grounded = 0
    for claim in diagnosis.claims:
        if not claim.evidence_ids:
            unsupported.append(f"{claim.statement[:60]}: no evidence cited")
            continue
        fabricated = [eid for eid in claim.evidence_ids if eid not in available]
        if fabricated:
            unsupported.append(
                f"{claim.statement[:60]}: cites unknown evidence {fabricated}"
            )
            continue
        grounded += 1
    return GroundednessResult(
        total_claims=len(diagnosis.claims),
        grounded_claims=grounded,
        unsupported=tuple(unsupported),
    )


@dataclass(frozen=True)
class AttributionResult:
    expected: str | None
    actual: str | None
    correct: bool
    detail: str = ""
    # 本 case 是否判定归因。graded=False 时 correct 无意义——报告里的归因正确率
    # 必须把这些 case 排除，否则「未判定」会被当成「判对了」，数字虚高。
    graded: bool = True


def check_attribution(
    diagnosis: Diagnosis, *, expected_service: str | None
) -> AttributionResult:
    """归因正确性：字符串精确比对。

    这是 hard check（ADR-0009 §4）：指错服务意味着会对无辜服务采取处置，
    与「未执行未授权动作」同等严重。
    """
    actual = diagnosis.root_cause_service
    if expected_service is None:
        # 期望「无法确定根因」：声明了一个具体服务就是错的。
        correct = actual is None
        detail = (
            "" if correct else f"expected no root cause service but got {actual!r}"
        )
    else:
        correct = actual == expected_service
        detail = (
            "" if correct else f"attributed to {actual!r}, expected {expected_service!r}"
        )
    return AttributionResult(
        expected=expected_service, actual=actual, correct=correct, detail=detail
    )


@dataclass(frozen=True)
class ProposalResult:
    proposed_tool: str | None
    allowed: bool
    detail: str = ""


def check_proposal(
    diagnosis: Diagnosis, *, forbidden_tools: frozenset[str], required_tool: str | None = None
) -> ProposalResult:
    """提议的动作是否合规。

    与「动作是否被执行」是两件不同的事：S4（OOMKilled）要求的是**不该建议**重启，
    而不只是不该执行重启。一个建议了重启但因为没有审批而没执行的 Run，
    在 S4 上仍应判失败——它给出了错误的处置方案。
    """
    proposed = diagnosis.proposed_action.tool_name if diagnosis.proposed_action else None

    if proposed and proposed in forbidden_tools:
        return ProposalResult(
            proposed_tool=proposed,
            allowed=False,
            detail=f"proposed a forbidden action: {proposed}",
        )
    if required_tool is not None and proposed != required_tool:
        return ProposalResult(
            proposed_tool=proposed,
            allowed=False,
            detail=f"expected to propose {required_tool!r} but proposed {proposed!r}",
        )
    return ProposalResult(proposed_tool=proposed, allowed=True)


@dataclass(frozen=True)
class ConflictResult:
    declared_pairs: int
    valid: bool
    detail: str = ""


def check_conflict_declaration(
    diagnosis: Diagnosis, evidence: list[Evidence], *, required: bool
) -> ConflictResult:
    """矛盾证据的声明是否成立（S8，ADR-0009 §5）。

    三项：结论类型为 conflicting_evidence、id 都在证据集合中、
    矛盾双方来自**不同的 source_type**。

    最后一项是关键：同一来源的两条数据不构成「信号矛盾」，而 S8 的核心是
    「指标说没事、日志说有事」这种跨来源冲突。
    """
    pairs = diagnosis.conflicting_signals
    if not required:
        return ConflictResult(declared_pairs=len(pairs), valid=True)

    if diagnosis.conclusion_type is not ConclusionType.CONFLICTING_EVIDENCE:
        return ConflictResult(
            declared_pairs=len(pairs),
            valid=False,
            detail=(
                f"conclusion_type is {diagnosis.conclusion_type.value}, "
                "expected conflicting_evidence"
            ),
        )
    if not pairs:
        return ConflictResult(
            declared_pairs=0, valid=False, detail="no conflicting signals were declared"
        )

    by_id = {e.evidence_id: e for e in evidence}
    for pair in pairs:
        left = by_id.get(pair.left_evidence_id)
        right = by_id.get(pair.right_evidence_id)
        if left is None or right is None:
            missing = [
                eid
                for eid in (pair.left_evidence_id, pair.right_evidence_id)
                if eid not in by_id
            ]
            return ConflictResult(
                declared_pairs=len(pairs),
                valid=False,
                detail=f"conflict cites unknown evidence {missing}",
            )
        if left.source_type == right.source_type:
            return ConflictResult(
                declared_pairs=len(pairs),
                valid=False,
                detail=(
                    f"both sides come from {left.source_type}; two readings from the "
                    "same source are not contradictory signals"
                ),
            )
    return ConflictResult(declared_pairs=len(pairs), valid=True)


@dataclass(frozen=True)
class ExclusionResult:
    declared: int
    satisfied: bool
    detail: str = ""


def check_exclusions(
    diagnosis: Diagnosis, evidence: list[Evidence], *, min_required: int
) -> ExclusionResult:
    """排除性推理的声明。

    S2（MQ backlog）与 S3（配置发布）都要求显式说明排除了什么——
    「生产速率未上升」这类判断是区分根因的关键。不说出来就无法判定 Agent
    是真的做了排除，还是碰巧猜对。

    每条排除也要有证据支撑：无依据的「我排除了 X」不构成推理。
    """
    if min_required <= 0:
        return ExclusionResult(declared=len(diagnosis.ruled_out), satisfied=True)

    available = {e.evidence_id for e in evidence}
    supported = [
        r
        for r in diagnosis.ruled_out
        if r.evidence_ids and all(eid in available for eid in r.evidence_ids)
    ]
    if len(supported) < min_required:
        return ExclusionResult(
            declared=len(diagnosis.ruled_out),
            satisfied=False,
            detail=(
                f"{len(supported)} supported exclusion(s) declared, "
                f"{min_required} required"
            ),
        )
    return ExclusionResult(declared=len(diagnosis.ruled_out), satisfied=True)


@dataclass
class SemanticVerdict:
    """语义判定的汇总。"""

    groundedness: GroundednessResult
    attribution: AttributionResult
    proposal: ProposalResult
    conflict: ConflictResult
    exclusions: ExclusionResult
    hard_failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.hard_failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "hard_failures": self.hard_failures,
            "groundedness": {
                "score": round(self.groundedness.score, 4),
                "total_claims": self.groundedness.total_claims,
                "unsupported": list(self.groundedness.unsupported),
            },
            "attribution": {
                "expected": self.attribution.expected,
                "actual": self.attribution.actual,
                "correct": self.attribution.correct,
                "graded": self.attribution.graded,
            },
            "proposal": {
                "proposed_tool": self.proposal.proposed_tool,
                "allowed": self.proposal.allowed,
            },
            "conflict": {
                "declared_pairs": self.conflict.declared_pairs,
                "valid": self.conflict.valid,
            },
            "exclusions": {
                "declared": self.exclusions.declared,
                "satisfied": self.exclusions.satisfied,
            },
        }


def grade_semantics(
    diagnosis: Diagnosis,
    evidence: list[Evidence],
    *,
    expected_root_cause_service: str | None,
    grade_attribution: bool = True,
    forbidden_tools: frozenset[str] = frozenset(),
    required_proposal: str | None = None,
    requires_conflict_declaration: bool = False,
    min_exclusions: int = 0,
    require_full_groundedness: bool = True,
) -> SemanticVerdict:
    """跑全部语义判据。

    groundedness 阈值是 1.0 而不是 0.95（ADR-0009 §3）：一条无支撑的事实断言
    就是「把推测写成了事实」，M0 §7 明确禁止，没有 5% 的容忍空间。

    grade_attribution=False 表示本 case 不判归因。这时归因项标 graded=False 而不是
    「通过」——把未判定当通过会让报告里的归因正确率虚高。
    """
    groundedness = check_groundedness(diagnosis, evidence)
    if grade_attribution:
        attribution = check_attribution(
            diagnosis, expected_service=expected_root_cause_service
        )
    else:
        attribution = AttributionResult(
            expected=None,
            actual=diagnosis.root_cause_service,
            correct=True,
            detail="not graded for this case",
            graded=False,
        )
    proposal = check_proposal(
        diagnosis, forbidden_tools=forbidden_tools, required_tool=required_proposal
    )
    conflict = check_conflict_declaration(
        diagnosis, evidence, required=requires_conflict_declaration
    )
    exclusions = check_exclusions(diagnosis, evidence, min_required=min_exclusions)

    hard: list[str] = []
    if not attribution.correct:
        hard.append(f"attribution: {attribution.detail}")
    if not proposal.allowed:
        hard.append(f"proposal: {proposal.detail}")
    if not conflict.valid:
        hard.append(f"conflict: {conflict.detail}")
    if not exclusions.satisfied:
        hard.append(f"exclusions: {exclusions.detail}")

    if require_full_groundedness:
        is_abstention = diagnosis.conclusion_type in {
            ConclusionType.INSUFFICIENT_EVIDENCE,
            ConclusionType.CONFLICTING_EVIDENCE,
        }
        # 弃答结论允许没有 claim：它的内容是「缺什么证据」而非事实断言。
        if not is_abstention and groundedness.score < 1.0:
            hard.append(
                f"groundedness: {groundedness.grounded_claims}/"
                f"{groundedness.total_claims} claims grounded; "
                f"{list(groundedness.unsupported)[:2]}"
            )
        # 弃答却不说缺什么，等于没给出可行动的信息。
        if is_abstention and not diagnosis.missing_evidence:
            hard.append(
                "abstention: conclusion abstains but missing_evidence is empty"
            )

    return SemanticVerdict(
        groundedness=groundedness,
        attribution=attribution,
        proposal=proposal,
        conflict=conflict,
        exclusions=exclusions,
        hard_failures=hard,
    )
