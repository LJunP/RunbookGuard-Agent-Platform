"""Trace 与 Replay 的测试（M6）。

核心不变量：
  1. 摘要**不含**墙上时钟——否则 Replay 永远「不一致」，这个检查就永远不会发现
     真正的不确定性来源。
  2. 行为变了摘要必须变——否则 Replay 永远「一致」，同样是空话。
  3. Secret 不进 Trace（M0 INV-6）。
  4. 摘要不同时 compare() 必须能指出**哪里**不同；指不出来要报「比对不完整」。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from agent_runtime.evaluation.dev_cases import BY_ID, DEV_CASES
from agent_runtime.evaluation.harness import EvaluationHarness, HarnessConfig
from agent_runtime.evaluation.trace import (
    TRACE_SCHEMA_VERSION,
    RunTrace,
    TraceEvidence,
    TraceStep,
    compare,
)
from agent_runtime.redaction import MASK
from agent_runtime.retrieval.service import RetrievalService

from test_harness_semantics import LAB, _mount_lab

CORPUS = Path(__file__).resolve().parents[3] / "datasets" / "runbooks"


@pytest.fixture(scope="module")
def retrieval() -> RetrievalService:
    return RetrievalService.lexical_only(CORPUS)


def _trace(**overrides) -> RunTrace:
    defaults = dict(
        run_id="run-1",
        case_id="dev-example",
        terminal_state="COMPLETE",
        failure_class=None,
        steps=[
            TraceStep(sequence=1, node="COLLECT_CONTEXT"),
            TraceStep(sequence=2, node="EXECUTING_TOOL", tool_name="get_service_metrics"),
        ],
        evidence=[
            TraceEvidence(
                evidence_id="ev-abc123",
                source_type="get_service_metrics",
                source_identity="service:synthetic-orders",
                content_hash="a" * 64,
                untrusted=True,
            )
        ],
        diagnosis={"conclusion_type": "diagnosis", "root_cause": "pool exhausted"},
        config_fingerprint="cfg-1",
    )
    defaults.update(overrides)
    return RunTrace(**defaults)


class TestDigestStability:
    def test_digest_ignores_run_id(self) -> None:
        """Replay 用新的 run_id 跑同一个 case。算进摘要会让它必然不同。"""
        assert _trace(run_id="run-1").digest() == _trace(run_id="run-99").digest()

    def test_digest_ignores_recorded_at(self) -> None:
        a = _trace(recorded_at="2026-08-29T10:00:00+00:00")
        b = _trace(recorded_at="2026-08-30T23:59:59+00:00")
        assert a.digest() == b.digest()

    def test_digest_is_stable_across_repeated_calls(self) -> None:
        trace = _trace()
        assert len({trace.digest() for _ in range(10)}) == 1

    def test_digest_ignores_timestamps_inside_step_detail(self) -> None:
        """deadline_exceeded 的 detail 带绝对时间，两次重跑必然不同。

        不规范化掉它，Replay 就永远报「不一致」，也就永远不会发现真正的
        不确定性来源——第一次跑 Replay 时正是这样暴露的。
        """
        a = _trace(
            steps=[
                TraceStep(
                    sequence=1,
                    node="FAILED",
                    detail="deadline 2026-08-29T13:29:12.525615+00:00 reached at "
                    "2026-08-29T13:29:13.525630+00:00",
                    failure_code="deadline_exceeded",
                )
            ]
        )
        b = _trace(
            steps=[
                TraceStep(
                    sequence=1,
                    node="FAILED",
                    detail="deadline 2026-08-30T09:00:00.111111+00:00 reached at "
                    "2026-08-30T09:00:01.222222+00:00",
                    failure_code="deadline_exceeded",
                )
            ]
        )
        assert a.digest() == b.digest()

    def test_digest_still_distinguishes_different_deny_reasons(self) -> None:
        """时间戳规范化不能把「拒绝理由不同」也一起吃掉。"""
        a = _trace(
            steps=[
                TraceStep(
                    sequence=1, node="POLICY_CHECK", detail="denied at 2026-08-29T10:00:00+00:00",
                    failure_code="resource_not_permitted",
                )
            ]
        )
        b = _trace(
            steps=[
                TraceStep(
                    sequence=1, node="POLICY_CHECK", detail="denied at 2026-08-29T10:00:00+00:00",
                    failure_code="not_in_allowlist",
                )
            ]
        )
        assert a.digest() != b.digest()

    def test_digest_changes_when_terminal_state_changes(self) -> None:
        assert _trace().digest() != _trace(terminal_state="FAILED").digest()

    def test_digest_changes_when_failure_class_changes(self) -> None:
        assert _trace().digest() != _trace(failure_class="deadline_exceeded").digest()

    def test_digest_changes_when_a_deny_reason_appears(self) -> None:
        """「同一步但拒绝理由变了」必须改变摘要。"""
        changed = _trace(
            steps=[
                TraceStep(sequence=1, node="COLLECT_CONTEXT"),
                TraceStep(
                    sequence=2,
                    node="POLICY_CHECK",
                    tool_name="get_service_metrics",
                    failure_code="resource_not_permitted",
                ),
            ]
        )
        assert _trace().digest() != changed.digest()

    def test_digest_changes_when_evidence_set_changes(self) -> None:
        changed = _trace(evidence=[])
        assert _trace().digest() != changed.digest()

    def test_digest_changes_when_conclusion_type_changes(self) -> None:
        changed = _trace(diagnosis={"conclusion_type": "insufficient_evidence"})
        assert _trace().digest() != changed.digest()


class TestRedaction:
    def test_secret_in_step_detail_is_masked(self) -> None:
        trace = _trace(
            steps=[
                TraceStep(
                    sequence=1,
                    node="OBSERVE",
                    detail="upstream replied api_key=sk-abcdefghijklmnopqrstuvwxyz",
                )
            ]
        )
        payload = json.dumps(trace.as_dict())
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in payload
        assert MASK in payload

    def test_secret_in_diagnosis_is_masked(self) -> None:
        trace = _trace(
            diagnosis={
                "conclusion_type": "diagnosis",
                "root_cause": "config contained password=hunter22",
                "claims": [{"statement": "token: ghp_0123456789abcdefghijkl", "evidence_ids": []}],
            }
        )
        payload = json.dumps(trace.as_dict())
        assert "hunter22" not in payload
        assert "ghp_0123456789abcdefghijkl" not in payload

    def test_secret_in_evidence_identity_is_masked(self) -> None:
        trace = _trace(
            evidence=[
                TraceEvidence(
                    evidence_id="ev-1",
                    source_type="search_service_logs",
                    source_identity="query:authorization=Bearer abcdefghijklmnop",
                    content_hash="b" * 64,
                    untrusted=True,
                )
            ]
        )
        assert "abcdefghijklmnop" not in json.dumps(trace.as_dict())


class TestRoundTrip:
    def test_write_and_load_preserves_digest(self, tmp_path: Path) -> None:
        trace = _trace()
        path = trace.write(tmp_path / "trace.json")
        assert RunTrace.load(path).digest() == trace.digest()

    def test_unknown_schema_version_is_rejected(self) -> None:
        """不尝试兼容旧版：被误读的 trace 会让 Replay 给出错误的一致性结论。"""
        raw = _trace().as_dict()
        raw["schema_version"] = "0"
        with pytest.raises(ValueError, match="schema version"):
            RunTrace.from_dict(raw)

    def test_current_schema_version_round_trips(self) -> None:
        raw = _trace().as_dict()
        assert raw["schema_version"] == TRACE_SCHEMA_VERSION
        RunTrace.from_dict(raw)


class TestCompare:
    def test_identical_traces_compare_equal(self) -> None:
        diff = compare(_trace(), _trace(run_id="run-2"))
        assert diff.identical
        assert diff.differences == ()

    def test_terminal_state_difference_is_reported(self) -> None:
        diff = compare(_trace(), _trace(terminal_state="FAILED"))
        assert not diff.identical
        assert any("terminal_state" in d for d in diff.differences)

    def test_step_level_difference_is_located(self) -> None:
        changed = _trace(
            steps=[
                TraceStep(sequence=1, node="COLLECT_CONTEXT"),
                TraceStep(
                    sequence=2, node="POLICY_CHECK", failure_code="not_in_allowlist"
                ),
            ]
        )
        diff = compare(_trace(), changed)
        assert not diff.identical
        assert any(d.startswith("step ") for d in diff.differences)

    def test_evidence_difference_is_reported(self) -> None:
        diff = compare(_trace(), _trace(evidence=[]))
        assert any("evidence ids" in d for d in diff.differences)

    def test_different_config_fingerprint_voids_the_comparison(self) -> None:
        """配置不同而行为不同是正常的，那不是不确定性。"""
        diff = compare(_trace(), _trace(config_fingerprint="cfg-2"))
        assert not diff.identical
        assert any("config fingerprint" in d for d in diff.differences)

    def test_conclusion_type_difference_is_reported(self) -> None:
        diff = compare(
            _trace(), _trace(diagnosis={"conclusion_type": "conflicting_evidence"})
        )
        assert any("conclusion_type" in d for d in diff.differences)


class TestHarnessReplay:
    """同一 case 连跑两次，trace 摘要必须一致。

    这是「跑一次」这条评测纪律的前提：如果同一配置的两次运行行为不同，
    那么一次运行的结果就不能代表系统的行为。
    """

    @respx.mock
    async def test_replay_of_every_case_is_identical(self, retrieval) -> None:
        _mount_lab()

        async def run_all():
            async with httpx.AsyncClient() as client:
                harness = EvaluationHarness(
                    retrieval=retrieval,
                    config=HarnessConfig(lab_base_url=LAB),
                    http_client=client,
                    config_fingerprint="test-cfg",
                )
                # deadline 类 case 的终态依赖「现在是否已过 deadline」，
                # 两次跑都过期，因此行为仍然一致。
                await harness.run_all(DEV_CASES)
                return harness.traces

        first = await run_all()
        second = await run_all()

        drifted = {}
        for case_id, trace in first.items():
            diff = compare(trace, second[case_id])
            if not diff.identical:
                drifted[case_id] = diff.differences
        assert not drifted, drifted

    @respx.mock
    async def test_trace_records_deny_reasons(self, retrieval) -> None:
        """审计要能回答「它当时被拒绝了什么」。"""
        _mount_lab()
        async with httpx.AsyncClient() as client:
            harness = EvaluationHarness(
                retrieval=retrieval,
                config=HarnessConfig(lab_base_url=LAB),
                http_client=client,
            )
            await harness.run_case(BY_ID["dev-cross-tenant-denied"])
        trace = harness.traces["dev-cross-tenant-denied"]
        codes = {s.failure_code for s in trace.steps if s.failure_code}
        assert "resource_not_permitted" in codes

    @respx.mock
    async def test_trace_records_injected_evidence(self, retrieval) -> None:
        """注入发生过这件事必须在 trace 里可见——审计需要知道。"""
        _mount_lab()
        async with httpx.AsyncClient() as client:
            harness = EvaluationHarness(
                retrieval=retrieval,
                config=HarnessConfig(lab_base_url=LAB),
                http_client=client,
            )
            await harness.run_case(BY_ID["dev-injection-no-escalation"])
        trace = harness.traces["dev-injection-no-escalation"]
        assert any(e.source_type == "search_service_logs" for e in trace.evidence)
        assert all(e.untrusted for e in trace.evidence)


class TestOtelCorrelation:
    """Trace ↔ span 互查的桥（M6 §10 A6 的「关系」部分）。

    只存 id 不存 span：span 归观测栈，Trace 归审计。
    """

    def test_no_tracer_yields_none(self) -> None:
        from agent_runtime.evaluation.trace import current_otel_trace_id

        # 测试进程没有配置 tracer provider；即便配了非记录性的默认 provider，
        # span 也是 invalid，同样返回 None。
        assert current_otel_trace_id() is None

    def test_trace_id_not_in_digest(self) -> None:
        """trace_id 每次运行必然不同。算进 digest 会让 Replay 永远报不一致，
        与 run_id 被排除是同一个道理。"""
        from agent_runtime.evaluation import trace as trace_mod

        base = trace_mod.RunTrace(
            run_id="r1",
            case_id="c",
            terminal_state="COMPLETE",
            failure_class=None,
            steps=[],
            evidence=[],
            diagnosis=None,
        )
        with_id = trace_mod.RunTrace(
            run_id="r1",
            case_id="c",
            terminal_state="COMPLETE",
            failure_class=None,
            steps=[],
            evidence=[],
            diagnosis=None,
            otel_trace_id="a" * 32,
        )
        assert base.digest() == with_id.digest()

    def test_trace_id_round_trips(self, tmp_path) -> None:
        from agent_runtime.evaluation import trace as trace_mod

        t = trace_mod.RunTrace(
            run_id="r1",
            case_id="c",
            terminal_state="COMPLETE",
            failure_class=None,
            steps=[],
            evidence=[],
            diagnosis=None,
            otel_trace_id="b" * 32,
        )
        loaded = trace_mod.RunTrace.load(t.write(tmp_path / "t.json"))
        assert loaded.otel_trace_id == "b" * 32
