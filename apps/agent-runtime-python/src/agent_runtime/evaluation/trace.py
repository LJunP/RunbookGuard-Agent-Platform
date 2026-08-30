"""Trace 与 Replay（DEV_PROMPT §12 M6 交付物）。

Trace 是一次 Run 的完整可复查记录：每一步的节点、工具、拒绝原因、证据引用、
终态、结论。它的用途有三个，缺一不可：

  1. **审计**——事后回答「它当时看到了什么、被拒绝了什么、凭什么下的结论」。
  2. **Replay**——同一份冻结配置重跑一次，比对 trace 摘要是否一致。
     不一致就说明有未声明的不确定性来源，那时「跑一次」的评测结论不成立。
  3. **归因**——case 失败时，从 trace 能定位到是哪一步偏了，而不用重跑加日志。

两个刻意的设计：

**摘要不含墙上时钟。** 时间戳会让每次重跑的摘要都不同，Replay 就永远「不一致」，
这个检查也就永远不会发现真正的不确定性。时间戳仍记录在 trace 里供人阅读，
只是不进 digest。

**全部文本过 redact()。** Trace 会被写盘、贴进 Gate 报告、可能进 Artifact，
是 M0 INV-6 明确点名的出口（Secret 绝不进入 Prompt / Trace / 日志 / Artifact / 报告）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..agent.bounded_loop import LoopOutcome
from ..redaction import redact

TRACE_SCHEMA_VERSION = "1"

# 摘要计算前把绝对时间戳规范化掉。
#
# 起因：deadline_exceeded 的 step detail 是「deadline <t1> reached at <t2>」，
# 两次重跑的 t1/t2 必然不同，摘要因此永远不一致，Replay 检查就永远不会发现
# 真正的不确定性来源（第一次跑 Replay 时就是这样暴露的）。
#
# 只规范化时间戳而不是整个丢掉 detail：detail 记录了「被哪条规则拒绝」，
# 丢掉会让「同一步但拒绝理由变了」被当成一致。
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)
_TIMESTAMP_PLACEHOLDER = "<timestamp>"


def normalise_for_digest(text: str) -> str:
    return _TIMESTAMP.sub(_TIMESTAMP_PLACEHOLDER, text)


@dataclass(frozen=True)
class TraceStep:
    sequence: int
    node: str
    detail: str = ""
    tool_name: str | None = None
    failure_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "node": self.node,
            "detail": redact(self.detail) or "",
            "tool_name": self.tool_name,
            "failure_code": self.failure_code,
        }

    def digest_tuple(self) -> tuple:
        """摘要用的形状。

        含 detail：它记录了「被哪条规则拒绝」「哪个工具失败」，排除掉会让
        「同一步但拒绝理由变了」被当成一致。detail 里的绝对时间戳先规范化掉。
        """
        return (
            self.sequence,
            self.node,
            normalise_for_digest(redact(self.detail) or ""),
            self.tool_name,
            self.failure_code,
        )


@dataclass(frozen=True)
class TraceEvidence:
    evidence_id: str
    source_type: str
    source_identity: str
    content_hash: str
    untrusted: bool
    document_id: str | None = None
    document_version: str | None = None
    section_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            # source_identity 可能含查询串，查询串可能被注入内容影响。
            "source_identity": redact(self.source_identity) or "",
            "content_hash": self.content_hash,
            "untrusted": self.untrusted,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "section_id": self.section_id,
        }

    def digest_tuple(self) -> tuple:
        return (
            self.evidence_id,
            self.source_type,
            self.content_hash,
            self.document_version,
            self.section_id,
        )


@dataclass
class RunTrace:
    run_id: str
    case_id: str
    terminal_state: str
    failure_class: str | None
    steps: list[TraceStep]
    evidence: list[TraceEvidence]
    diagnosis: dict[str, Any] | None
    awaiting_approval_id: str | None = None
    # 冻结配置的指纹。Replay 比对前必须先确认两次跑的是同一套配置——
    # 配置不同而 trace 不同是正常的，那不是不确定性。
    config_fingerprint: str = ""
    recorded_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    schema_version: str = TRACE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "config_fingerprint": self.config_fingerprint,
            "recorded_at": self.recorded_at,
            "terminal_state": self.terminal_state,
            "failure_class": self.failure_class,
            "awaiting_approval_id": self.awaiting_approval_id,
            "steps": [s.as_dict() for s in self.steps],
            "evidence": [e.as_dict() for e in self.evidence],
            "diagnosis": _redact_payload(self.diagnosis),
            "digest": self.digest(),
        }

    def digest(self) -> str:
        """行为摘要。**不含**时间戳与 run_id。

        run_id 排除在外的理由：Replay 用新的 run_id 跑同一个 case，
        把它算进去会让摘要必然不同。
        """
        payload = {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "terminal_state": self.terminal_state,
            "failure_class": self.failure_class,
            "steps": [list(s.digest_tuple()) for s in self.steps],
            "evidence": [list(e.digest_tuple()) for e in self.evidence],
            "diagnosis": _normalise_payload(_redact_payload(self.diagnosis)),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> RunTrace:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RunTrace:
        version = raw.get("schema_version")
        if version != TRACE_SCHEMA_VERSION:
            # 不尝试兼容旧版：一个被误读的 trace 会让 Replay 给出错误的一致性结论，
            # 那比读不出来更危险（与 checkpoint 版本不兼容时不硬恢复同一个道理）。
            raise ValueError(
                f"trace schema version {version!r} != {TRACE_SCHEMA_VERSION!r}"
            )
        return cls(
            run_id=raw["run_id"],
            case_id=raw["case_id"],
            terminal_state=raw["terminal_state"],
            failure_class=raw.get("failure_class"),
            steps=[
                TraceStep(
                    sequence=s["sequence"],
                    node=s["node"],
                    detail=s.get("detail", ""),
                    tool_name=s.get("tool_name"),
                    failure_code=s.get("failure_code"),
                )
                for s in raw.get("steps", [])
            ],
            evidence=[
                TraceEvidence(
                    evidence_id=e["evidence_id"],
                    source_type=e["source_type"],
                    source_identity=e.get("source_identity", ""),
                    content_hash=e["content_hash"],
                    untrusted=e.get("untrusted", True),
                    document_id=e.get("document_id"),
                    document_version=e.get("document_version"),
                    section_id=e.get("section_id"),
                )
                for e in raw.get("evidence", [])
            ],
            diagnosis=raw.get("diagnosis"),
            awaiting_approval_id=raw.get("awaiting_approval_id"),
            config_fingerprint=raw.get("config_fingerprint", ""),
            recorded_at=raw.get("recorded_at", ""),
        )


def trace_from_outcome(
    *,
    case_id: str,
    run_id: str,
    outcome: LoopOutcome,
    config_fingerprint: str = "",
) -> RunTrace:
    return RunTrace(
        run_id=run_id,
        case_id=case_id,
        terminal_state=outcome.terminal_status.value,
        failure_class=outcome.failure_class,
        steps=[
            TraceStep(
                sequence=s.sequence,
                node=s.node.value,
                detail=s.detail,
                tool_name=s.tool_name,
                failure_code=s.failure_code,
            )
            for s in outcome.steps
        ],
        evidence=[
            TraceEvidence(
                evidence_id=e.evidence_id,
                source_type=e.source_type,
                source_identity=e.source_identity,
                content_hash=e.content_hash,
                untrusted=e.untrusted,
                document_id=e.document_id,
                document_version=e.document_version,
                section_id=e.section_id,
            )
            for e in outcome.evidence
        ],
        diagnosis=outcome.diagnosis,
        awaiting_approval_id=outcome.awaiting_approval_id,
        config_fingerprint=config_fingerprint,
    )


@dataclass(frozen=True)
class ReplayDiff:
    """一次 Replay 的比对结果。"""

    case_id: str
    identical: bool
    expected_digest: str
    actual_digest: str
    differences: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "identical": self.identical,
            "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest,
            "differences": list(self.differences),
        }


def compare(expected: RunTrace, actual: RunTrace) -> ReplayDiff:
    """比对两次 Run 的行为。

    先比摘要，再逐项定位差异——只报「摘要不同」等于要求人自己去 diff 两个 JSON，
    而 Replay 的价值恰恰是**指出哪里不同**。
    """
    differences: list[str] = []

    if expected.config_fingerprint and actual.config_fingerprint:
        if expected.config_fingerprint != actual.config_fingerprint:
            differences.append(
                "config fingerprint differs: the two runs used different frozen configs, "
                "so a behavioural difference is expected and this comparison is void"
            )

    if expected.terminal_state != actual.terminal_state:
        differences.append(
            f"terminal_state {expected.terminal_state} -> {actual.terminal_state}"
        )
    if expected.failure_class != actual.failure_class:
        differences.append(
            f"failure_class {expected.failure_class!r} -> {actual.failure_class!r}"
        )

    exp_ids = [e.evidence_id for e in expected.evidence]
    act_ids = [e.evidence_id for e in actual.evidence]
    if exp_ids != act_ids:
        differences.append(f"evidence ids {exp_ids} -> {act_ids}")

    exp_steps = [s.digest_tuple() for s in expected.steps]
    act_steps = [s.digest_tuple() for s in actual.steps]
    if len(exp_steps) != len(act_steps):
        differences.append(f"step count {len(exp_steps)} -> {len(act_steps)}")
    for index, (left, right) in enumerate(zip(exp_steps, act_steps)):
        if left != right:
            differences.append(f"step {index}: {left} -> {right}")

    exp_conclusion = (expected.diagnosis or {}).get("conclusion_type")
    act_conclusion = (actual.diagnosis or {}).get("conclusion_type")
    if exp_conclusion != act_conclusion:
        differences.append(f"conclusion_type {exp_conclusion!r} -> {act_conclusion!r}")

    expected_digest = expected.digest()
    actual_digest = actual.digest()
    if expected_digest != actual_digest and not differences:
        # 摘要不同但逐项比对没找出差异，说明比对项少于摘要项。
        # 报出来而不是判「一致」：静默的漏检会让 Replay 变成一句空话。
        differences.append(
            "digests differ but no compared field explains it; the comparison is "
            "incomplete relative to the digest"
        )

    return ReplayDiff(
        case_id=expected.case_id,
        identical=expected_digest == actual_digest and not differences,
        expected_digest=expected_digest,
        actual_digest=actual_digest,
        differences=tuple(differences),
    )


def _redact_payload(payload: Any) -> Any:
    """递归脱敏。结论里的自由文本可能带上工具结果里的内容。"""
    if payload is None:
        return None
    if isinstance(payload, str):
        return redact(payload)
    if isinstance(payload, dict):
        return {k: _redact_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_redact_payload(v) for v in payload]
    return payload


def _normalise_payload(payload: Any) -> Any:
    """递归规范化时间戳。只在算摘要时用，不影响写盘的可读内容。"""
    if isinstance(payload, str):
        return normalise_for_digest(payload)
    if isinstance(payload, dict):
        return {k: _normalise_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_normalise_payload(v) for v in payload]
    return payload
