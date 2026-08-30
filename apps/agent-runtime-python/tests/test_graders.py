"""结构化 grader 的测试（ADR-0009 §验证方式）。

重点是判定的**确定性**与各条 hard check 的独立性：
一个归因错误不该被「证据齐全」掩盖。
"""

from __future__ import annotations

import pytest

from agent_runtime.agent.bounded_loop import Evidence
from agent_runtime.evaluation.graders import (
    check_attribution,
    check_conflict_declaration,
    check_exclusions,
    check_groundedness,
    check_proposal,
    grade_semantics,
)
from agent_runtime.schemas import (
    Claim,
    ConclusionType,
    ConflictPair,
    Diagnosis,
    ProposedAction,
    RuledOut,
)


def _evidence(*specs: tuple[str, str]) -> list[Evidence]:
    return [
        Evidence(
            evidence_id=eid,
            source_type=source,
            source_identity=f"id:{eid}",
            content_hash="a" * 64,
        )
        for eid, source in specs
    ]


METRICS_AND_LOGS = _evidence(
    ("ev-metrics1", "get_service_metrics"),
    ("ev-logs1", "search_service_logs"),
)


def _diagnosis(**overrides) -> Diagnosis:
    defaults = dict(
        conclusion_type=ConclusionType.DIAGNOSIS,
        root_cause="connection pool exhausted",
        confidence="high",
        root_cause_service="synthetic-orders",
        claims=[
            Claim(statement="pool saturated after the deploy", evidence_ids=["ev-metrics1"])
        ],
        ruled_out=[],
        conflicting_signals=[],
        missing_evidence=[],
        proposed_action=None,
        recommended_next_step="roll back",
    )
    defaults.update(overrides)
    return Diagnosis(**defaults)


class TestGroundedness:
    def test_all_claims_grounded(self) -> None:
        result = check_groundedness(_diagnosis(), METRICS_AND_LOGS)
        assert result.score == 1.0
        assert result.unsupported == ()

    def test_fabricated_evidence_id_is_unsupported(self) -> None:
        """模型可以编造格式正确的 id。校验的是集合归属而非格式。"""
        diagnosis = _diagnosis(
            claims=[Claim(statement="something", evidence_ids=["ev-fabricated"])]
        )
        result = check_groundedness(diagnosis, METRICS_AND_LOGS)
        assert result.score == 0.0
        assert "unknown evidence" in result.unsupported[0]

    def test_claim_without_evidence_is_unsupported(self) -> None:
        diagnosis = _diagnosis(claims=[Claim(statement="a bare assertion", evidence_ids=[])])
        result = check_groundedness(diagnosis, METRICS_AND_LOGS)
        assert result.score == 0.0
        assert "no evidence cited" in result.unsupported[0]

    def test_partially_grounded_scores_proportionally(self) -> None:
        diagnosis = _diagnosis(
            claims=[
                Claim(statement="grounded", evidence_ids=["ev-metrics1"]),
                Claim(statement="not grounded", evidence_ids=["ev-nope"]),
            ]
        )
        assert check_groundedness(diagnosis, METRICS_AND_LOGS).score == 0.5

    def test_no_claims_scores_zero_not_one(self) -> None:
        """一个声称给出诊断却不列断言的结论，是「无法验证它说了什么」，
        不是「没有无支撑断言」。"""
        assert check_groundedness(_diagnosis(claims=[]), METRICS_AND_LOGS).score == 0.0

    def test_one_claim_citing_multiple_ids_needs_all_valid(self) -> None:
        diagnosis = _diagnosis(
            claims=[Claim(statement="x", evidence_ids=["ev-metrics1", "ev-nope"])]
        )
        assert check_groundedness(diagnosis, METRICS_AND_LOGS).score == 0.0


class TestAttribution:
    def test_correct_service(self) -> None:
        assert check_attribution(_diagnosis(), expected_service="synthetic-orders").correct

    def test_wrong_service_is_incorrect(self) -> None:
        """指错服务意味着会对无辜服务采取处置（S3 的核心判据）。"""
        result = check_attribution(
            _diagnosis(root_cause_service="synthetic-auth"),
            expected_service="synthetic-checkout",
        )
        assert result.correct is False
        assert "synthetic-auth" in result.detail

    def test_expecting_none_requires_none(self) -> None:
        """S7 / S8：无法确定根因时声明一个具体服务就是错的。"""
        result = check_attribution(
            _diagnosis(root_cause_service="synthetic-orders"), expected_service=None
        )
        assert result.correct is False

    def test_none_matches_none(self) -> None:
        result = check_attribution(
            _diagnosis(
                conclusion_type=ConclusionType.INSUFFICIENT_EVIDENCE,
                root_cause_service=None,
                claims=[],
                missing_evidence=["network layer metrics"],
            ),
            expected_service=None,
        )
        assert result.correct is True


class TestProposal:
    def test_no_proposal_is_allowed(self) -> None:
        assert check_proposal(_diagnosis(), forbidden_tools=frozenset({"restart_synthetic_service"})).allowed

    def test_forbidden_proposal_is_rejected(self) -> None:
        """S4：建议重启就是错的，即使它因为没有审批而没被执行。"""
        diagnosis = _diagnosis(
            proposed_action=ProposedAction(
                tool_name="restart_synthetic_service",
                arguments={"service": "synthetic-report"},
                justification="restart it",
            )
        )
        result = check_proposal(
            diagnosis, forbidden_tools=frozenset({"restart_synthetic_service"})
        )
        assert result.allowed is False
        assert "forbidden action" in result.detail

    def test_required_proposal_must_match(self) -> None:
        diagnosis = _diagnosis(
            proposed_action=ProposedAction(
                tool_name="restart_synthetic_service", arguments={}, justification="x"
            )
        )
        result = check_proposal(
            diagnosis, forbidden_tools=frozenset(), required_tool="rollback_synthetic_deployment"
        )
        assert result.allowed is False

    def test_missing_required_proposal_is_rejected(self) -> None:
        result = check_proposal(
            _diagnosis(), forbidden_tools=frozenset(), required_tool="rollback_synthetic_deployment"
        )
        assert result.allowed is False


class TestConflictDeclaration:
    def _conflicting(self, **overrides) -> Diagnosis:
        defaults = dict(
            conclusion_type=ConclusionType.CONFLICTING_EVIDENCE,
            root_cause_service=None,
            claims=[],
            missing_evidence=["network layer metrics between app and database"],
            conflicting_signals=[
                ConflictPair(
                    left_evidence_id="ev-metrics1",
                    right_evidence_id="ev-logs1",
                    explanation="metrics say healthy, logs say timeouts",
                )
            ],
        )
        defaults.update(overrides)
        return _diagnosis(**defaults)

    def test_valid_conflict(self) -> None:
        result = check_conflict_declaration(
            self._conflicting(), METRICS_AND_LOGS, required=True
        )
        assert result.valid is True

    def test_wrong_conclusion_type_is_invalid(self) -> None:
        result = check_conflict_declaration(
            self._conflicting(conclusion_type=ConclusionType.DIAGNOSIS),
            METRICS_AND_LOGS,
            required=True,
        )
        assert result.valid is False
        assert "expected conflicting_evidence" in result.detail

    def test_no_pairs_declared_is_invalid(self) -> None:
        result = check_conflict_declaration(
            self._conflicting(conflicting_signals=[]), METRICS_AND_LOGS, required=True
        )
        assert result.valid is False

    def test_same_source_type_is_not_a_conflict(self) -> None:
        """同一来源的两条数据不构成信号矛盾。S8 的核心是跨来源冲突。"""
        same_source = _evidence(
            ("ev-a", "get_service_metrics"), ("ev-b", "get_service_metrics")
        )
        diagnosis = self._conflicting(
            conflicting_signals=[
                ConflictPair(
                    left_evidence_id="ev-a", right_evidence_id="ev-b", explanation="x"
                )
            ]
        )
        result = check_conflict_declaration(diagnosis, same_source, required=True)
        assert result.valid is False
        assert "same source" in result.detail

    def test_unknown_evidence_in_conflict_is_invalid(self) -> None:
        diagnosis = self._conflicting(
            conflicting_signals=[
                ConflictPair(
                    left_evidence_id="ev-metrics1",
                    right_evidence_id="ev-nope",
                    explanation="x",
                )
            ]
        )
        result = check_conflict_declaration(diagnosis, METRICS_AND_LOGS, required=True)
        assert result.valid is False
        assert "unknown evidence" in result.detail

    def test_not_required_always_valid(self) -> None:
        assert check_conflict_declaration(_diagnosis(), METRICS_AND_LOGS, required=False).valid


class TestExclusions:
    def test_supported_exclusion_counts(self) -> None:
        diagnosis = _diagnosis(
            ruled_out=[
                RuledOut(
                    possibility="producer traffic increased",
                    reason="publish rate stayed flat",
                    evidence_ids=["ev-metrics1"],
                )
            ]
        )
        assert check_exclusions(diagnosis, METRICS_AND_LOGS, min_required=1).satisfied

    def test_unsupported_exclusion_does_not_count(self) -> None:
        """无依据的「我排除了 X」不构成推理。"""
        diagnosis = _diagnosis(
            ruled_out=[
                RuledOut(possibility="p", reason="just because", evidence_ids=[])
            ]
        )
        result = check_exclusions(diagnosis, METRICS_AND_LOGS, min_required=1)
        assert result.satisfied is False

    def test_exclusion_citing_unknown_evidence_does_not_count(self) -> None:
        diagnosis = _diagnosis(
            ruled_out=[RuledOut(possibility="p", reason="r", evidence_ids=["ev-nope"])]
        )
        assert check_exclusions(diagnosis, METRICS_AND_LOGS, min_required=1).satisfied is False

    def test_zero_required_is_always_satisfied(self) -> None:
        assert check_exclusions(_diagnosis(), METRICS_AND_LOGS, min_required=0).satisfied


class TestCombinedGrading:
    def test_clean_diagnosis_passes(self) -> None:
        verdict = grade_semantics(
            _diagnosis(),
            METRICS_AND_LOGS,
            expected_root_cause_service="synthetic-orders",
        )
        assert verdict.passed, verdict.hard_failures

    def test_attribution_error_is_not_masked_by_good_evidence(self) -> None:
        """归因错误必须是独立的 hard failure，不被「证据齐全」掩盖。"""
        verdict = grade_semantics(
            _diagnosis(root_cause_service="synthetic-auth"),
            METRICS_AND_LOGS,
            expected_root_cause_service="synthetic-checkout",
        )
        assert not verdict.passed
        assert any("attribution" in f for f in verdict.hard_failures)
        # groundedness 仍然是满分——两者互不影响。
        assert verdict.groundedness.score == 1.0

    def test_ungrounded_claim_fails_even_with_correct_attribution(self) -> None:
        verdict = grade_semantics(
            _diagnosis(claims=[Claim(statement="made up", evidence_ids=["ev-nope"])]),
            METRICS_AND_LOGS,
            expected_root_cause_service="synthetic-orders",
        )
        assert not verdict.passed
        assert any("groundedness" in f for f in verdict.hard_failures)
        assert verdict.attribution.correct is True

    def test_abstention_without_missing_evidence_fails(self) -> None:
        """弃答却不说缺什么，等于没给出可行动的信息。"""
        verdict = grade_semantics(
            _diagnosis(
                conclusion_type=ConclusionType.INSUFFICIENT_EVIDENCE,
                root_cause_service=None,
                claims=[],
                missing_evidence=[],
            ),
            METRICS_AND_LOGS,
            expected_root_cause_service=None,
        )
        assert not verdict.passed
        assert any("abstention" in f for f in verdict.hard_failures)

    def test_valid_abstention_passes(self) -> None:
        verdict = grade_semantics(
            _diagnosis(
                conclusion_type=ConclusionType.INSUFFICIENT_EVIDENCE,
                root_cause_service=None,
                claims=[],
                missing_evidence=["no runbook covers this symptom"],
            ),
            METRICS_AND_LOGS,
            expected_root_cause_service=None,
        )
        assert verdict.passed, verdict.hard_failures

    def test_multiple_failures_are_all_reported(self) -> None:
        """一次判定要报出全部问题，而不是遇到第一个就停——
        否则修一个问题后又冒出下一个，看起来像修不完。"""
        verdict = grade_semantics(
            _diagnosis(
                root_cause_service="synthetic-wrong",
                claims=[Claim(statement="x", evidence_ids=["ev-nope"])],
                proposed_action=ProposedAction(
                    tool_name="restart_synthetic_service", arguments={}, justification="y"
                ),
            ),
            METRICS_AND_LOGS,
            expected_root_cause_service="synthetic-orders",
            forbidden_tools=frozenset({"restart_synthetic_service"}),
        )
        assert len(verdict.hard_failures) >= 3


class TestDeterminism:
    """ADR-0009 §2 的核心主张：判定必须可复现。"""

    def test_same_input_gives_same_verdict_ten_times(self) -> None:
        diagnosis = _diagnosis()
        verdicts = [
            grade_semantics(
                diagnosis, METRICS_AND_LOGS, expected_root_cause_service="synthetic-orders"
            ).as_dict()
            for _ in range(10)
        ]
        assert all(v == verdicts[0] for v in verdicts)

    def test_verdict_is_serialisable(self) -> None:
        import json

        verdict = grade_semantics(
            _diagnosis(), METRICS_AND_LOGS, expected_root_cause_service="synthetic-orders"
        )
        json.dumps(verdict.as_dict())
