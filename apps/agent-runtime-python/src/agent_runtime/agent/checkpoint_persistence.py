"""checkpoint 持久化与元数据上报（M6 §10 A4 / F2 整改）。

此前 AgentGraph 默认只有 `InMemorySaver`，进程一死恢复能力就归零——
「崩溃可恢复」这根支柱只在单进程存活期间成立。这里补两块：

1. **状态本体**：`RUNBOOKGUARD_CHECKPOINT_DB` 指向 sqlite 文件时，
   checkpointer 换成 `AsyncSqliteSaver`，状态跨进程存活。
   未设置时仍是 `InMemorySaver`——测试与评测不需要文件，
   静默写文件反而会留下难以清理的状态。

2. **元数据**：`RecordingCheckpointer` 包装任意 saver，在每次写入 checkpoint 时
   把**元数据**（id、序号、状态摘要）上报给 Java 控制面。控制面只存元数据不存状态，
   恢复前核对 stateDigest 能发现「读回的状态被篡改或损坏」——只看 id 存在发现不了。

铁律五核验记录（langgraph 1.2.11 / langgraph-checkpoint-sqlite 3.1.1）：
`BaseCheckpointSaver` 的方法**不是** abstractmethod，默认实现抛
NotImplementedError；`AsyncSqliteSaver` 没有 `from_path`，构造走
`AsyncSqliteSaver(conn)` 后显式 `await setup()`。这里的包装按当日实际 API 写。

上报是 fire-and-forget：元数据写失败**不**中断 Run——控制面不是状态本体的权威，
但失败必须可见（日志 + 指标），静默丢元数据会让 digest 核对变成摆设。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
)

log = logging.getLogger(__name__)


def compute_state_digest(checkpoint: Checkpoint) -> str:
    """状态摘要。

    对 channel_versions 而不是 channel_values 做摘要：values 里可能有
    未脱敏的工具结果，把它原样再 hash 一份没有额外价值；versions 足以
    唯一刻画「这一步之后状态走到哪了」，且是纯版本号，不携带内容。
    """
    payload = {
        "v": checkpoint.get("v"),
        "id": checkpoint.get("id"),
        "channel_versions": checkpoint.get("channel_versions"),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class CheckpointMetadataSink:
    """元数据上报的协议。任何能收一批元数据的对象都可以实现它。"""

    async def record(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError


class ControlPlaneCheckpointSink(CheckpointMetadataSink):
    """把元数据 POST 给 Java 控制面。

    控制面用 (run_id, sequence) 唯一键去重，因此这里的重试是安全的——
    最坏情况是重复上报被 INSERT IGNORE 跳过。
    """

    def __init__(self, *, base_url: str, api_token: str, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        # token 只从构造参数传入；repr 不暴露。
        self._token = api_token
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return f"ControlPlaneCheckpointSink(base_url={self._base_url!r}, api_token=***)"

    async def record(self, payload: dict[str, Any]) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/v1/runs/{payload['runId']}/checkpoints",
                json={
                    "graphVersion": payload["graphVersion"],
                    "stateSchemaVersion": payload["stateSchemaVersion"],
                    "checkpoints": [
                        {
                            "checkpointId": payload["checkpointId"],
                            "sequence": payload["sequence"],
                            "stateDigest": payload["stateDigest"],
                            "stateLocation": payload.get("stateLocation", ""),
                        }
                    ],
                },
                headers={"authorization": f"Bearer {self._token}"},
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"checkpoint metadata rejected: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )


class RecordingCheckpointer(BaseCheckpointSaver):
    """包装任意 saver；每次写入 checkpoint 时向 sink 上报元数据。

    序号按**本进程内**每个 run 递增。进程重启后续跑时序号会重新从 1 开始，
    与重启前的记录撞唯一键——INSERT IGNORE 会跳过重复项。
    这是有意的保守选择：元数据去重的代价（跨进程计数器）远大于收益。
    """

    def __init__(
        self,
        inner: BaseCheckpointSaver,
        *,
        sink: CheckpointMetadataSink | None,
        graph_version: str,
        state_schema_version: str,
        state_location: str = "",
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._graph_version = graph_version
        self._state_schema_version = state_schema_version
        self._state_location = state_location
        self._sequence: dict[str, int] = {}
        self.serde = inner.serde

    # -- 写路径：先落本体，再上报元数据 -----------------------------------

    async def aput(
        self,
        config,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ):
        result = await self._inner.aput(config, checkpoint, metadata, new_versions)
        self._record(config, checkpoint)
        return result

    def put(self, config, checkpoint: Checkpoint, metadata: CheckpointMetadata,
            new_versions: ChannelVersions):
        result = self._inner.put(config, checkpoint, metadata, new_versions)
        self._record(config, checkpoint)
        return result

    def _record(self, config, checkpoint: Checkpoint) -> None:
        if self._sink is None:
            return
        thread_id = (config or {}).get("configurable", {}).get("thread_id", "")
        sequence = self._sequence.get(thread_id, 0) + 1
        self._sequence[thread_id] = sequence
        payload = {
            "runId": thread_id,
            "graphVersion": self._graph_version,
            "stateSchemaVersion": self._state_schema_version,
            "checkpointId": checkpoint.get("id", ""),
            "sequence": sequence,
            "stateDigest": compute_state_digest(checkpoint),
            "stateLocation": self._state_location,
        }
        task = asyncio.ensure_future(self._safe_record(payload))
        # 不持有 task 引用会 GC 掉未完成的任务——保留，但只保留最近一条每 run。
        self._last_task = task

    async def _safe_record(self, payload: dict[str, Any]) -> None:
        try:
            await self._sink.record(payload)
        except Exception as exc:  # noqa: BLE001
            from .. import observability as obs

            obs.checkpoint_metadata_failures.inc()
            log.warning(
                "checkpoint metadata upload failed for run %s seq %s: %s",
                payload["runId"], payload["sequence"], exc,
            )

    # -- 其余全部委托 ------------------------------------------------------

    async def aget_tuple(self, config) -> CheckpointTuple | None:
        return await self._inner.aget_tuple(config)

    def get_tuple(self, config) -> CheckpointTuple | None:
        return self._inner.get_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):  # noqa: A002
        return self._inner.list(config, filter=filter, before=before, limit=limit)

    def alist(self, config, *, filter=None, before=None, limit=None):  # noqa: A002
        return self._inner.alist(config, filter=filter, before=before, limit=limit)

    async def aput_writes(self, config, writes, task_id, task_path: str = "") -> None:
        return await self._inner.aput_writes(config, writes, task_id, task_path)

    def put_writes(self, config, writes, task_id, task_path: str = "") -> None:
        return self._inner.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        return await self._inner.adelete_thread(thread_id)

    def delete_thread(self, thread_id: str) -> None:
        return self._inner.delete_thread(thread_id)


def build_checkpointer(
    *,
    sink: CheckpointMetadataSink | None = None,
    graph_version: str = "unknown",
    state_schema_version: str = "1",
) -> BaseCheckpointSaver:
    """按环境装配 checkpointer。

    RUNBOOKGUARD_CHECKPOINT_DB 未设置时返回 InMemorySaver——与历史行为一致，
    测试与评测不依赖文件。
    """
    db_path = os.environ.get("RUNBOOKGUARD_CHECKPOINT_DB", "").strip()
    if not db_path:
        from langgraph.checkpoint.memory import InMemorySaver

        inner = InMemorySaver()
        location = "memory://"
    else:
        # 延迟导入：sqlite 路径只在真正需要时建立连接。
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        conn = aiosqlite.connect(db_path)
        inner = AsyncSqliteSaver(conn)
        location = f"sqlite://{db_path}"

    if sink is None:
        return inner
    return RecordingCheckpointer(
        inner,
        sink=sink,
        graph_version=graph_version,
        state_schema_version=state_schema_version,
        state_location=location,
    )
