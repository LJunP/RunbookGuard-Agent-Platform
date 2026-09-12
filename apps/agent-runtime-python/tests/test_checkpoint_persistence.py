"""checkpoint 持久化与元数据上报的测试（F2 整改）。

按铁律五，针对 langgraph 1.2.11 的实际 API 写：
BaseCheckpointSaver 的方法不是 abstractmethod（默认抛 NotImplementedError），
AsyncSqliteSaver 没有 from_path，构造走 AsyncSqliteSaver(conn) + await setup()。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runtime.agent.checkpoint_persistence import (
    RecordingCheckpointer,
    build_checkpointer,
    compute_state_digest,
)


class FakeInner:
    """记录调用的小替身。BaseCheckpointSaver 的方法都是委托目标。"""

    def __init__(self) -> None:
        self.stored: list[tuple] = []
        self.aput_calls = 0
        # RecordingCheckpointer 会透传 serde；给一个最小替身。
        self.serde = json

    async def aput(self, config, checkpoint, metadata, new_versions):
        self.aput_calls += 1
        self.stored.append((config, checkpoint))
        return config

    def put(self, config, checkpoint, metadata, new_versions):
        self.stored.append((config, checkpoint))
        return config

    async def aget_tuple(self, config):
        return None

    def get_tuple(self, config):
        return None

    def list(self, config, *, filter=None, before=None, limit=None):  # noqa: A002
        return iter([])

    def alist(self, config, *, filter=None, before=None, limit=None):  # noqa: A002
        async def gen():
            yield None
        return gen()

    async def aput_writes(self, config, writes, task_id, task_path="") -> None:
        return None

    def put_writes(self, config, writes, task_id, task_path="") -> None:
        return None

    async def adelete_thread(self, thread_id: str) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        return None


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: list[dict] = []
        self.fail = fail

    async def record(self, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("control plane unreachable")
        self.payloads.append(payload)


def _checkpoint(cid: str = "ckpt-1") -> dict:
    return {
        "v": 1,
        "id": cid,
        "ts": "2026-08-30T00:00:00+00:00",
        "channel_versions": {"status": 3},
        "channel_values": {},
        "versions_seen": {},
    }


def _config(run_id: str = "run-1") -> dict:
    # AsyncSqliteSaver 要求 configurable 里带 checkpoint_ns / checkpoint_id
    # （真实图运行时由 LangGraph 填）。缺了它们 saver 侧会 KeyError，
    # 与 RecordingCheckpointer 无关。
    return {
        "configurable": {
            "thread_id": run_id,
            "checkpoint_ns": "",
            "checkpoint_id": None,
        }
    }


def _wrapper(sink=None, inner=None) -> tuple[RecordingCheckpointer, FakeInner, RecordingSink]:
    i = inner or FakeInner()
    s = sink if sink is not None else RecordingSink()
    w = RecordingCheckpointer(
        i,
        sink=s,
        graph_version="langgraph-v1",
        state_schema_version="1",
        state_location="sqlite://test.db",
    )
    return w, i, s


class TestRecordingCheckpointer:
    async def test_aput_delegates_and_reports_metadata(self) -> None:
        w, inner, sink = _wrapper()
        await w.aput(_config(), _checkpoint(), {}, {})
        assert inner.aput_calls == 1
        # 上报是 fire-and-forget 的 task，等一个事件循环周期让它落地。
        import asyncio

        await asyncio.sleep(0)
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})
        assert len(sink.payloads) == 1
        p = sink.payloads[0]
        assert p["runId"] == "run-1"
        assert p["checkpointId"] == "ckpt-1"
        assert p["sequence"] == 1
        assert p["graphVersion"] == "langgraph-v1"
        assert len(p["stateDigest"]) == 64

    async def test_sequence_increments_per_run(self) -> None:
        w, _, sink = _wrapper()
        for seq in (1, 2, 3):
            await w.aput(_config(), _checkpoint(f"ckpt-{seq}"), {}, {})
        import asyncio

        await asyncio.sleep(0)
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})
        assert [p["sequence"] for p in sink.payloads] == [1, 2, 3]

    async def test_different_runs_have_independent_sequences(self) -> None:
        w, _, sink = _wrapper()
        await w.aput(_config("run-a"), _checkpoint(), {}, {})
        await w.aput(_config("run-b"), _checkpoint(), {}, {})
        import asyncio

        await asyncio.sleep(0)
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})
        assert [p["sequence"] for p in sink.payloads] == [1, 1]
        assert {p["runId"] for p in sink.payloads} == {"run-a", "run-b"}

    async def test_sink_failure_does_not_break_the_write(self) -> None:
        """元数据上报失败不能中断 Run——控制面不是状态本体的权威。
        但失败必须可见（计数器），不能静默。"""
        from agent_runtime import observability as obs

        w, inner, _ = _wrapper(sink=RecordingSink(fail=True))
        result = await w.aput(_config(), _checkpoint(), {}, {})
        assert result is not None and inner.aput_calls == 1
        import asyncio

        await asyncio.sleep(0)
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})
        assert obs.checkpoint_metadata_failures._value.get() > 0

    async def test_get_tuple_and_writes_delegate(self) -> None:
        w, inner, _ = _wrapper()
        assert await w.aget_tuple(_config()) is None
        assert w.get_tuple(_config()) is None
        await w.aput_writes(_config(), [], "task")
        w.put_writes(_config(), [], "task")
        await w.adelete_thread("t")
        w.delete_thread("t")
        list(w.list(_config()))
        # alist 是 async 生成器，走一遍确认委托没断。
        async for _ in w.alist(_config()):
            pass

    async def test_no_sink_means_no_reporting(self) -> None:
        w, inner, _ = _wrapper(sink=None)
        await w.aput(_config(), _checkpoint(), {}, {})
        assert inner.aput_calls == 1


class TestStateDigest:
    def test_digest_covers_id_and_versions(self) -> None:
        a = compute_state_digest(_checkpoint("ckpt-1"))
        b = compute_state_digest(_checkpoint("ckpt-2"))
        assert a != b

    def test_digest_is_stable_for_same_checkpoint(self) -> None:
        assert compute_state_digest(_checkpoint()) == compute_state_digest(_checkpoint())

    def test_digest_is_64_hex(self) -> None:
        d = compute_state_digest(_checkpoint())
        assert len(d) == 64
        int(d, 16)


class TestBuildCheckpointer:
    async def test_default_is_in_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from langgraph.checkpoint.memory import InMemorySaver

        monkeypatch.delenv("RUNBOOKGUARD_CHECKPOINT_DB", raising=False)
        cp = build_checkpointer()
        assert isinstance(cp, InMemorySaver)

    async def test_sqlite_when_db_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        db = tmp_path / "checkpoints.db"
        monkeypatch.setenv("RUNBOOKGUARD_CHECKPOINT_DB", str(db))
        cp = build_checkpointer()
        assert isinstance(cp, AsyncSqliteSaver)
        # setup 建表；跑一次确认文件真的能被初始化。
        await cp.setup()

    async def test_sqlite_roundtrip_survives_reopen(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """「跨进程恢复」的直接验证：写一个 checkpoint，关掉连接，
        用新连接读回——拿到同一个 checkpoint id。"""
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        db = tmp_path / "checkpoints.db"
        monkeypatch.setenv("RUNBOOKGUARD_CHECKPOINT_DB", str(db))

        config = _config("run-reopen")
        checkpoint = _checkpoint("ckpt-persist")

        saver = AsyncSqliteSaver(aiosqlite.connect(str(db)))
        await saver.setup()
        await saver.aput(config, checkpoint, {}, {})
        await saver.conn.commit()
        await saver.conn.close()

        saver2 = AsyncSqliteSaver(aiosqlite.connect(str(db)))
        await saver2.setup()
        got = await saver2.aget_tuple(config)
        await saver2.conn.close()
        assert got is not None
        assert got.checkpoint["id"] == "ckpt-persist"

    async def test_wrapper_over_sqlite_reports_and_persists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        db = tmp_path / "checkpoints.db"
        monkeypatch.setenv("RUNBOOKGUARD_CHECKPOINT_DB", str(db))
        sink = RecordingSink()

        saver = AsyncSqliteSaver(aiosqlite.connect(str(db)))
        await saver.setup()
        wrapped = RecordingCheckpointer(
            saver,
            sink=sink,
            graph_version="langgraph-v1",
            state_schema_version="1",
            state_location=f"sqlite://{db}",
        )
        await wrapped.aput(_config("run-both"), _checkpoint("ckpt-both"), {}, {})
        await saver.conn.commit()
        got = await saver.aget_tuple(_config("run-both"))
        await saver.conn.close()
        assert got.checkpoint["id"] == "ckpt-both"

        import asyncio

        await asyncio.sleep(0)
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})
        assert len(sink.payloads) == 1
        assert sink.payloads[0]["stateLocation"] == f"sqlite://{db}"
