"""把 Runtime 的执行轨迹上报给 Java 控制面（M7 集成缺口补全）。

**缺口**：`POST /api/v1/runs/{id}/trace` 端点在 M7 就绪，但只有冒烟脚本在调。
真实执行路径（有界循环 / LangGraph）产出的步骤与证据只存在于 Python 进程内存里，
从未上报——于是控制台的 Trace 视图对最有含金量的那些 Run 是空白的：
0 步骤、0 证据，审批与终态孤零零挂着。

本模块补上这条单向链路：把 LoopOutcome / GraphOutcome 的内容整形成
控制面 TraceController 的请求体并 POST。它是**上报**，不是权威存储的转移：
执行历史在本进程仍然完整，控制面多一份是为了崩溃后可查、控制台可看。

role 用 AGENT_RUNTIME：上报轨迹是 Worker 职责的一部分（与审批请求同一身份）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _step_status(node: str, failure_code: str | None) -> str:
    if failure_code is None:
        return "COMPLETED"
    # POLICY_CHECK 上带失败码是**成功的拦截**；其余节点带失败码才是真失败。
    if node == "POLICY_CHECK":
        return "DENIED"
    return "FAILED"


def _step_from_loop(s: Any) -> dict[str, Any]:
    return {
        "sequence": s.sequence,
        "nodeName": s.node.value,
        "status": _step_status(s.node.value, s.failure_code),
        "toolCallId": s.tool_name,
        "failureClass": s.failure_code,
        "outputArtifact": (s.detail[:512] if s.detail else None) or None,
    }


def _step_from_graph(s: dict[str, Any]) -> dict[str, Any]:
    node = s.get("node", "")
    return {
        "sequence": s.get("sequence", 0),
        "nodeName": node,
        "status": _step_status(node, s.get("failure_code")),
        "toolCallId": s.get("tool_name"),
        "failureClass": s.get("failure_code"),
        "outputArtifact": (s.get("detail", "")[:512] or None),
    }


def _evidence_from_loop(e: Any) -> dict[str, Any]:
    return {
        "evidenceId": e.evidence_id,
        "sourceType": e.source_type,
        "sourceIdentity": e.source_identity or e.source_type,
        "version": e.document_version or "runtime-local",
        "location": e.section_id or e.source_identity or e.source_type,
        "contentHash": e.content_hash,
    }


def _evidence_from_graph(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidenceId": e["evidence_id"],
        "sourceType": e["source_type"],
        "sourceIdentity": e.get("source_identity") or e["source_type"],
        "version": e.get("document_version") or "runtime-local",
        "location": e.get("section_id") or e.get("source_identity") or e["source_type"],
        "contentHash": e["content_hash"],
    }


def _evidence_ok(item: dict[str, Any]) -> bool:
    """控制面对 evidence 有强校验（contentHash 必须 64 位十六进制）。
    不满足的条目跳过并记日志，而不是让整批上报失败——
    缺一条证据的轨迹仍然比没有轨迹可查。"""
    import re

    if not re.fullmatch(r"[0-9a-f]{64}", item.get("contentHash", "")):
        log.warning(
            "evidence %s has non-64-hex contentHash %r; skipped in report",
            item.get("evidenceId"), item.get("contentHash", ""),
        )
        return False
    return True


async def report_loop_outcome(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_token: str,
    run_id: str,
    outcome: Any,
) -> bool:
    """上报有界循环（LoopOutcome）的轨迹。返回是否成功。

    失败只记日志不抛异常：上报是旁路，不该把一次本已成功的执行
    变成失败——但每次失败都会出现在日志与演练输出里。
    """
    body = {
        "steps": [_step_from_loop(s) for s in outcome.steps],
        "evidence": [
            item for item in (_evidence_from_loop(e) for e in outcome.evidence)
            if _evidence_ok(item)
        ],
    }
    return await _post(client, base_url, api_token, run_id, body)


async def report_graph_outcome(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_token: str,
    run_id: str,
    outcome: Any,
) -> bool:
    """上报 LangGraph（GraphOutcome）的轨迹。"""
    body = {
        "steps": [_step_from_graph(s) for s in outcome.steps],
        "evidence": [
            item for item in (_evidence_from_graph(e) for e in outcome.evidence)
            if _evidence_ok(item)
        ],
    }
    return await _post(client, base_url, api_token, run_id, body)


async def _post(
    client: httpx.AsyncClient,
    base_url: str,
    api_token: str,
    run_id: str,
    body: dict[str, Any],
) -> bool:
    try:
        response = await client.post(
            f"{base_url}/api/v1/runs/{run_id}/trace",
            json=body,
            headers={"authorization": f"Bearer {api_token}"},
        )
    except httpx.HTTPError as exc:
        log.warning("trace report for run %s failed: %s", run_id, exc)
        return False
    if response.status_code != 200:
        log.warning(
            "trace report for run %s rejected: HTTP %s %s",
            run_id, response.status_code, response.text[:200],
        )
        return False
    return True


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---- Worker 生命周期上报 -------------------------------------------------
#
# Trace 步骤只是档案的一半。Run 行本身的 status / steps_used / 终态
# 由 Worker 协议维护（lease → progress → terminal）。不走这个协议的执行者
# 会让 Run 永远停在 CREATED 0/25——控制台上看起来像空的，即使轨迹已入库
# （用户实际点击时暴露）。

_worker_counter = {"n": 0}


async def acquire_lease(
    *, client: httpx.AsyncClient, base_url: str, api_token: str, run_id: str
) -> int:
    """以 Worker 身份获取 lease，返回 fencing token。失败抛异常：
    没有租约的执行者不该上报任何东西。"""
    _worker_counter["n"] += 1
    worker_id = f"drill-worker-{_worker_counter['n']}"
    response = await client.post(
        f"{base_url}/api/v1/worker/runs/{run_id}/lease",
        json={"workerId": worker_id},
        headers={"authorization": f"Bearer {api_token}"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"lease failed: HTTP {response.status_code} {response.text[:200]}")
    token = response.json().get("fencingToken")
    if not token:
        raise RuntimeError(f"lease response missing fencingToken: {response.text[:200]}")
    return int(token)


async def settle_terminal(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_token: str,
    run_id: str,
    worker_id: str,
    fencing_token: int,
    terminal_status: str,
    failure_class: str | None = None,
) -> bool:
    """结算终态（幂等端点）。AWAITING_APPROVAL 不是终态，调用方应跳过。"""
    body: dict[str, Any] = {
        "workerId": worker_id,
        "fencingToken": fencing_token,
        "terminalStatus": terminal_status,
    }
    if failure_class:
        body["failureClass"] = failure_class
    try:
        response = await client.post(
            f"{base_url}/api/v1/worker/runs/{run_id}/terminal",
            json=body,
            headers={"authorization": f"Bearer {api_token}"},
        )
    except httpx.HTTPError as exc:
        log.warning("terminal settlement for run %s failed: %s", run_id, exc)
        return False
    if response.status_code != 200:
        log.warning(
            "terminal settlement for run %s rejected: HTTP %s %s",
            run_id, response.status_code, response.text[:200],
        )
        return False
    return True


async def report_run_lifecycle(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_token: str,
    run_id: str,
    outcome: Any,
    reporter: str,
) -> dict[str, bool]:
    """一站式：上报轨迹 + 结算终态。

    只对**终态**结算（COMPLETE / FAILED / BLOCKED）；挂起（AWAITING_APPROVAL）
    的 Run 本来就该停在半路，不结算。
    """
    ok_trace = (
        report_loop_outcome if reporter == "loop" else report_graph_outcome
    )
    ok_trace_result = await ok_trace(
        client=client, base_url=base_url, api_token=api_token, run_id=run_id, outcome=outcome
    )

    terminal = outcome.terminal_status.value
    results = {"trace": ok_trace_result, "progress": False, "terminal": False}
    if terminal in {"COMPLETE", "FAILED", "BLOCKED"}:
        fencing = await acquire_lease(
            client=client, base_url=base_url, api_token=api_token, run_id=run_id
        )
        worker_id = f"drill-worker-{_worker_counter['n']}"

        # 进度上报：Run 列表的「状态 / 步数」列读的是 agent_run 行本身，
        # 不走这一步的话即使轨迹已入库，列表仍显示 CREATED 0/25——
        # 看起来像空 Run（用户实际点击时暴露）。
        steps = getattr(outcome, "steps", [])
        tool_calls = sum(
            1 for s in steps
            if (s.node.value if hasattr(s.node, "value") else s.node) == "EXECUTING_TOOL"
            and getattr(s, "failure_code", None) is None
        )
        try:
            progress_response = await client.post(
                f"{base_url}/api/v1/worker/runs/{run_id}/progress",
                json={
                    "workerId": worker_id,
                    "fencingToken": fencing,
                    "status": terminal,
                    "stepsUsed": len(steps),
                    "toolCallCount": tool_calls,
                },
                headers={"authorization": f"Bearer {api_token}"},
            )
            results["progress"] = progress_response.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("progress for run %s failed: %s", run_id, exc)

        results["terminal"] = await settle_terminal(
            client=client,
            base_url=base_url,
            api_token=api_token,
            run_id=run_id,
            worker_id=worker_id,
            fencing_token=fencing,
            terminal_status=terminal,
            failure_class=outcome.failure_class,
        )
    return results
