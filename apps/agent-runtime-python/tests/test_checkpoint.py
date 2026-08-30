"""Checkpoint 版本兼容校验的测试（M0 终止条件 7）。

补 M4 Gate 报告 §5.1 第 1 条的缺口：之前只有「注入 version_incompatible=True」的
单测，没有真实的版本不兼容场景。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_runtime.agent.checkpoint import (
    CheckpointCompatibilityGate,
    CheckpointRecord,
    Compatibility,
    InMemoryCheckpointStore,
    state_digest,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
STATE = {"status": "OBSERVE", "evidence": [{"evidence_id": "ev-abc"}], "round": 2}


def _record(**overrides) -> CheckpointRecord:
    defaults = dict(
        checkpoint_id="ckpt-1",
        run_id="run-1",
        tenant_id="tenant-demo",
        graph_version="langgraph-v1",
        state_schema_version="1",
        sequence=3,
        state_digest=state_digest(STATE),
        created_at=NOW,
    )
    defaults.update(overrides)
    return CheckpointRecord(**defaults)


def _gate(**overrides) -> CheckpointCompatibilityGate:
    defaults = dict(graph_version="langgraph-v1", state_schema_version="1")
    defaults.update(overrides)
    return CheckpointCompatibilityGate(**defaults)


class TestRecordConstruction:
    @pytest.mark.parametrize(
        "field",
        ["checkpoint_id", "run_id", "tenant_id", "graph_version",
         "state_schema_version", "state_digest"],
    )
    def test_empty_field_is_rejected(self, field: str) -> None:
        """版本字段不给默认值：默认 "1" 会让两个不同版本看起来同版本。"""
        with pytest.raises(ValueError, match=field):
            _record(**{field: ""})

    def test_negative_sequence_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sequence"):
            _record(sequence=-1)


class TestCompatible:
    def test_matching_versions_pass(self) -> None:
        verdict = _gate().evaluate(_record(), run_id="run-1", tenant_id="tenant-demo")
        assert verdict.compatible is True
        assert verdict.reason is Compatibility.COMPATIBLE
        assert verdict.failure_class is None

    def test_digest_check_passes_for_unchanged_state(self) -> None:
        verdict = _gate().evaluate(
            _record(), run_id="run-1", tenant_id="tenant-demo", observed_state=STATE
        )
        assert verdict.compatible is True


class TestIncompatible:
    """每一条都必须映射到 version_incompatible，且不加载状态。"""

    def test_missing_record(self) -> None:
        verdict = _gate().evaluate(None, run_id="run-1", tenant_id="tenant-demo")
        assert verdict.compatible is False
        assert verdict.reason is Compatibility.MISSING_RECORD
        assert verdict.failure_class == "version_incompatible"

    def test_graph_version_mismatch(self) -> None:
        """图结构变了：旧状态里的节点名可能已不存在，恢复后会走到不该走的分支。"""
        verdict = _gate(graph_version="langgraph-v2").evaluate(
            _record(), run_id="run-1", tenant_id="tenant-demo"
        )
        assert verdict.reason is Compatibility.GRAPH_VERSION_MISMATCH
        assert verdict.failure_class == "version_incompatible"
        assert "langgraph-v1" in verdict.detail and "langgraph-v2" in verdict.detail

    def test_state_schema_mismatch(self) -> None:
        verdict = _gate(state_schema_version="2").evaluate(
            _record(), run_id="run-1", tenant_id="tenant-demo"
        )
        assert verdict.reason is Compatibility.STATE_SCHEMA_MISMATCH
        assert verdict.failure_class == "version_incompatible"

    def test_run_id_mismatch(self) -> None:
        verdict = _gate().evaluate(
            _record(run_id="run-other"), run_id="run-1", tenant_id="tenant-demo"
        )
        assert verdict.compatible is False

    def test_tenant_mismatch_is_refused(self) -> None:
        """硬恢复会把别的租户的状态加载进来（威胁 T-4）。"""
        verdict = _gate().evaluate(
            _record(tenant_id="tenant-b"), run_id="run-1", tenant_id="tenant-demo"
        )
        assert verdict.reason is Compatibility.TENANT_MISMATCH
        assert verdict.failure_class == "version_incompatible"

    def test_tampered_state_is_detected(self) -> None:
        mutated = {**STATE, "round": 99}
        verdict = _gate().evaluate(
            _record(), run_id="run-1", tenant_id="tenant-demo", observed_state=mutated
        )
        assert verdict.reason is Compatibility.DIGEST_MISMATCH
        assert verdict.failure_class == "version_incompatible"


class TestNoSoftRecovery:
    """不做「向后兼容」的猜测。"""

    @pytest.mark.parametrize(
        "recorded,runtime",
        [
            ("langgraph-v1", "langgraph-v2"),
            ("langgraph-v2", "langgraph-v1"),
            ("bounded-loop-v1", "langgraph-v1"),
        ],
    )
    def test_any_graph_version_difference_is_fatal(self, recorded: str, runtime: str) -> None:
        verdict = _gate(graph_version=runtime).evaluate(
            _record(graph_version=recorded), run_id="run-1", tenant_id="tenant-demo"
        )
        assert verdict.compatible is False

    def test_newer_state_schema_is_not_accepted(self) -> None:
        """更新的 schema 也不接受：新字段的语义 runtime 不认识。"""
        verdict = _gate(state_schema_version="1").evaluate(
            _record(state_schema_version="2"), run_id="run-1", tenant_id="tenant-demo"
        )
        assert verdict.compatible is False


class TestStore:
    def test_latest_returns_highest_sequence(self) -> None:
        store = InMemoryCheckpointStore()
        store.append(_record(checkpoint_id="c1", sequence=1))
        store.append(_record(checkpoint_id="c3", sequence=3))
        store.append(_record(checkpoint_id="c2", sequence=2))
        assert store.latest("run-1").sequence == 3

    def test_duplicate_sequence_is_rejected(self) -> None:
        """同一 run 的同一序号只能有一条：两条会让「最新状态」有歧义。"""
        store = InMemoryCheckpointStore()
        store.append(_record(sequence=1))
        with pytest.raises(ValueError, match="already exists"):
            store.append(_record(checkpoint_id="other", sequence=1))

    def test_unknown_run_has_no_latest(self) -> None:
        assert InMemoryCheckpointStore().latest("run-missing") is None

    def test_history_is_ordered(self) -> None:
        store = InMemoryCheckpointStore()
        for seq in (5, 1, 3):
            store.append(_record(checkpoint_id=f"c{seq}", sequence=seq))
        assert [r.sequence for r in store.history("run-1")] == [1, 3, 5]


class TestStateDigest:
    def test_key_order_does_not_matter(self) -> None:
        assert state_digest({"a": 1, "b": 2}) == state_digest({"b": 2, "a": 1})

    def test_float_is_allowed(self) -> None:
        """与 ADR-0002 的参数摘要不同：图状态里可能有浮点（工具返回的指标值），
        而这个摘要不参与任何授权判定。"""
        assert state_digest({"value": 12.5})

    def test_change_alters_digest(self) -> None:
        assert state_digest({"a": 1}) != state_digest({"a": 2})
