"""Checkpoint 的版本兼容校验（M4 Gate 报告 §5.1 第 1 条的遗留缺口）。

M0 终止条件 7：graph / checkpoint 版本不兼容就走失败终态，**不硬恢复**。

为什么必须在恢复**之前**判定：LangGraph 的 checkpointer 不知道也不该知道我们的
graph_version 与 state_schema_version 兼容规则。一旦状态被加载进图，不兼容的字段
可能已经造成了错误的行为，那时再判定就晚了。

因此这一层是 LangGraph 之外的、独立的门禁。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class Compatibility(str, Enum):
    COMPATIBLE = "compatible"
    GRAPH_VERSION_MISMATCH = "graph_version_mismatch"
    STATE_SCHEMA_MISMATCH = "state_schema_mismatch"
    TENANT_MISMATCH = "tenant_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    MISSING_RECORD = "missing_record"


@dataclass(frozen=True)
class CheckpointRecord:
    """Control Plane 侧的 checkpoint 元记录。

    与 LangGraph 的 checkpoint **分开存**：这份记录只包含判定兼容性所需的字段，
    不含状态本身。状态在 LangGraph 的 checkpointer 里。

    分开的好处：判定不需要反序列化状态。一个不兼容的状态可能连反序列化都会失败，
    而我们要在那之前就拒绝。
    """

    checkpoint_id: str
    run_id: str
    tenant_id: str
    graph_version: str
    state_schema_version: str
    sequence: int
    state_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("checkpoint_id", "run_id", "tenant_id", "graph_version",
                     "state_schema_version", "state_digest"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")


@dataclass(frozen=True)
class CompatibilityVerdict:
    compatible: bool
    reason: Compatibility
    detail: str = ""

    @property
    def failure_class(self) -> str | None:
        """不兼容一律映射到 version_incompatible。

        tenant 不匹配也映射到它而不是 authorization_failed：这不是权限问题，
        是「这个 checkpoint 不属于这个 run」的完整性问题，硬恢复会把别的租户的
        状态加载进来。
        """
        return None if self.compatible else "version_incompatible"


def state_digest(state: dict[str, Any]) -> str:
    """状态摘要。

    不用 ADR-0002 的 JCS 规范化：那套算法拒绝浮点，而图状态里可能有浮点
    （例如工具返回的指标值）。这里的摘要只用于「状态是否被改动过」的检测，
    不参与任何授权判定，因此可以宽松一些。
    """
    payload = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class CheckpointCompatibilityGate:
    """恢复前的门禁。纯函数式：所有事实由参数传入。"""

    def __init__(self, *, graph_version: str, state_schema_version: str) -> None:
        self.graph_version = graph_version
        self.state_schema_version = state_schema_version

    def evaluate(
        self,
        record: CheckpointRecord | None,
        *,
        run_id: str,
        tenant_id: str,
        observed_state: dict[str, Any] | None = None,
    ) -> CompatibilityVerdict:
        if record is None:
            return CompatibilityVerdict(
                False,
                Compatibility.MISSING_RECORD,
                f"no checkpoint record for run {run_id}",
            )

        if record.run_id != run_id:
            return CompatibilityVerdict(
                False,
                Compatibility.TENANT_MISMATCH,
                f"checkpoint belongs to run {record.run_id}, not {run_id}",
            )

        if record.tenant_id != tenant_id:
            # 硬恢复会把别的租户的状态加载进来（威胁 T-4）。
            return CompatibilityVerdict(
                False,
                Compatibility.TENANT_MISMATCH,
                f"checkpoint belongs to tenant {record.tenant_id}, not {tenant_id}",
            )

        if record.graph_version != self.graph_version:
            # 不做「向后兼容」的猜测：图结构变了，旧状态里的节点名可能已不存在，
            # 恢复后会走到一个不该走的分支。
            return CompatibilityVerdict(
                False,
                Compatibility.GRAPH_VERSION_MISMATCH,
                f"checkpoint graph_version={record.graph_version} "
                f"but runtime is {self.graph_version}",
            )

        if record.state_schema_version != self.state_schema_version:
            return CompatibilityVerdict(
                False,
                Compatibility.STATE_SCHEMA_MISMATCH,
                f"checkpoint state_schema_version={record.state_schema_version} "
                f"but runtime is {self.state_schema_version}",
            )

        if observed_state is not None:
            actual = state_digest(observed_state)
            if actual != record.state_digest:
                return CompatibilityVerdict(
                    False,
                    Compatibility.DIGEST_MISMATCH,
                    f"state digest mismatch: recorded={record.state_digest[:16]} "
                    f"actual={actual[:16]}",
                )

        return CompatibilityVerdict(True, Compatibility.COMPATIBLE)


class InMemoryCheckpointStore:
    """checkpoint 元记录的存储。

    M5 用内存实现。真正的存储是 M1 已建的 MySQL `checkpoint` 表——接入它需要
    Agent Runtime 有写 Control Plane 的端点，那是 M7 的事。这里的接口形状按
    最终目标设计，因此替换实现时调用方不用改。
    """

    def __init__(self) -> None:
        self._by_run: dict[str, list[CheckpointRecord]] = {}

    def append(self, record: CheckpointRecord) -> None:
        history = self._by_run.setdefault(record.run_id, [])
        if any(r.sequence == record.sequence for r in history):
            raise ValueError(
                f"checkpoint sequence {record.sequence} already exists for run {record.run_id}"
            )
        history.append(record)
        history.sort(key=lambda r: r.sequence)

    def latest(self, run_id: str) -> CheckpointRecord | None:
        history = self._by_run.get(run_id)
        return history[-1] if history else None

    def history(self, run_id: str) -> list[CheckpointRecord]:
        return list(self._by_run.get(run_id, []))
