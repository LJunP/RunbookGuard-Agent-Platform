#!/usr/bin/env python3
"""M4 跨语言集成演练。

前面的测试都在 mock / 替身层面。这里连**真实的** Java Control Plane（MySQL + Redis）
与真实的 synthetic-lab，跑完整的 Agent 链路，证明 M4 Gate 的三条硬条件：

  1. 未审批的写动作 100% 不执行
  2. 审批后篡改参数被 digest 校验拦住
  3. AWAITING_APPROVAL 可跨进程恢复

跨语言这一步是必须的：Java 与 Python 各自算 arguments_digest，两侧算出不同值时
审批绑定就完全失效，而这个缺陷在单侧测试里永远看不到。

前置：deploy/compose 已 up 且全部 healthy。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "apps" / "agent-runtime-python" / "src"))

import httpx  # noqa: E402

from agent_runtime.agent.bounded_loop import BoundedAgentLoop, RunSpec  # noqa: E402
from agent_runtime.agent.graph import AgentGraph  # noqa: E402
from agent_runtime.agent.state import RunStatus  # noqa: E402
from agent_runtime.agent.termination import BoundedLoopGuard, BudgetState  # noqa: E402
from agent_runtime.approval.digest import digest as digest_fn  # noqa: E402
from agent_runtime.approval.gateway import ControlPlaneApprovalGateway  # noqa: E402
from agent_runtime.provider.fake import FakeProvider, FakeTurn  # noqa: E402
from agent_runtime.tools import catalogue  # noqa: E402
from agent_runtime.tools.action_executor import ActionToolExecutor  # noqa: E402
from agent_runtime.tools.contract import ToolSuggestion  # noqa: E402
from agent_runtime.tools.executor import ReadOnlyToolExecutor  # noqa: E402
from agent_runtime.tools.policy import PolicyEngine  # noqa: E402

CP = "http://127.0.0.1:8080"
# actuator 在独立管理端口（M7 §7.5 整改）。
CP_MGMT = "http://127.0.0.1:9080"
LAB = "http://127.0.0.1:8090"

OPERATOR = "dev-operator-token"
AGENT = "dev-agent-token"
APPROVER = "dev-approver-token"

# ADR-0009 之后 Diagnosis 的字段变了（新增 conclusion_type / claims /
# root_cause_service 等）。这个常量在 M6 改 schema 时没跟上，导致演练 1 与 7
# 落 provider_failure —— 那是**正确**的行为（schema 不符不许静默修补），
# 但让演练报出了假失败。
#
# claims 里的 evidence_ids 留空：真实 evidence_id 是内容摘要派生的，
# 演练脚本预知不了。groundedness 判据不在这个演练的范围内（那是 M6 的事），
# 这里只需要一个 schema 合法的产出。
DIAGNOSIS = json.dumps(
    {
        "conclusion_type": "diagnosis",
        "root_cause": "connection pool exhausted after the v1.5.0 deploy",
        "confidence": "high",
        "root_cause_service": "synthetic-orders",
        "claims": [],
        "ruled_out": [],
        "conflicting_signals": [],
        "missing_evidence": [],
        "proposed_action": None,
        "recommended_next_step": "roll back synthetic-orders to v1.4.2",
    }
)

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        print(f"ok    {label}")
        PASS += 1
    else:
        print(f"FAIL  {label}")
        if detail:
            print(f"      {detail}")
        FAIL += 1


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "content-type": "application/json"}


async def create_run(client: httpx.AsyncClient) -> tuple[str, str]:
    incident = await client.post(
        f"{CP}/api/v1/incidents",
        headers=_headers(OPERATOR),
        json={"source": "synthetic-lab", "severity": "P1", "title": "m4 integration drill"},
    )
    incident.raise_for_status()
    incident_id = incident.json()["incidentId"]

    run = await client.post(
        f"{CP}/api/v1/runs",
        headers=_headers(AGENT),
        json={
            "incidentId": incident_id,
            "graphVersion": AgentGraph.GRAPH_VERSION,
            "promptVersion": "p1",
            "modelId": "fake-provider",
            "datasetVersion": "incidents-dev-v1",
        },
    )
    run.raise_for_status()
    return incident_id, run.json()["runId"]


def _spec(run_id: str, *, allowed: frozenset[str]) -> RunSpec:
    return RunSpec(
        run_id=run_id,
        tenant_id="tenant-demo",
        principal_id="prin-agent",
        incident_summary="synthetic-orders p99 latency above 3s, error rate elevated",
        allowed_tool_names=allowed,
        permitted_resources=frozenset(
            {"svc:synthetic-orders", "svc:synthetic-report", "queue:synthetic-notify.work"}
        ),
    )


def _budget() -> BudgetState:
    return BudgetState(
        max_steps=60,
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        cost_budget_micros=500_000,
        token_budget=200_000,
        tool_call_budget=20,
    )


def _guard() -> BoundedLoopGuard:
    return BoundedLoopGuard(_budget(), now=lambda: datetime.now(UTC))


def _gateway(client: httpx.AsyncClient) -> ControlPlaneApprovalGateway:
    return ControlPlaneApprovalGateway(CP, api_token=AGENT, client=client)


READ_PLAN = [
    ToolSuggestion(
        tool_name="get_service_metrics",
        arguments={"service": "synthetic-orders", "metric": "db_pool_active"},
    ),
    ToolSuggestion(
        tool_name="search_service_logs",
        arguments={"service": "synthetic-orders", "query": "connection pool"},
    ),
    ToolSuggestion(
        tool_name="get_recent_deployments", arguments={"service": "synthetic-orders"}
    ),
]

ROLLBACK = ToolSuggestion(
    tool_name="rollback_synthetic_deployment",
    arguments={"service": "synthetic-orders", "target_version": "v1.4.2"},
)


async def drill_1_read_only_diagnosis(client: httpx.AsyncClient) -> None:
    """E2E 主线：真实取证 → 真实模型结构（fake provider）→ 结论。"""
    print("\n== 演练 1：只读诊断走通真实 synthetic-lab ==")
    _, run_id = await create_run(client)
    loop = BoundedAgentLoop(
        provider=FakeProvider(script=[FakeTurn(text=DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB, client=client),
        guard=_guard(),
        approvals=_gateway(client),
    )
    outcome = await loop.run(
        _spec(run_id, allowed=frozenset(t.name for t in catalogue.READ_ONLY_TOOLS)),
        READ_PLAN,
    )
    check("终态为 COMPLETE", outcome.terminal_status is RunStatus.COMPLETE,
          f"actual={outcome.terminal_status} failure={outcome.failure_class}")
    check("从真实 lab 取到 3 份证据", len(outcome.evidence) == 3,
          f"got {len(outcome.evidence)}")
    check("全部证据标记为不可信", all(e.untrusted for e in outcome.evidence))
    check("产出诊断结论", (outcome.diagnosis or {}).get("conclusion_type") == "diagnosis")

    serialized = json.dumps([e.__dict__ for e in outcome.evidence])
    check("证据中不含 is_decoy（诱饵标记已在工具层剥除）", "is_decoy" not in serialized)


async def drill_2_unapproved_write_never_executes(client: httpx.AsyncClient) -> None:
    """M4 Gate 硬条件 1。"""
    print("\n== 演练 2：未审批的写动作 100% 不执行 ==")
    _, run_id = await create_run(client)
    await client.post(f"{LAB}/v1/actions/reset")
    before = (await client.get(f"{LAB}/v1/service-state", params={"service": "synthetic-orders"})).json()

    loop = BoundedAgentLoop(
        provider=FakeProvider(script=[FakeTurn(text=DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB, client=client),
        guard=_guard(),
        approvals=_gateway(client),
    )
    outcome = await loop.run(
        _spec(run_id, allowed=frozenset({"rollback_synthetic_deployment"})), [ROLLBACK]
    )
    check("Run 挂起在 AWAITING_APPROVAL", outcome.terminal_status is RunStatus.AWAITING_APPROVAL,
          f"actual={outcome.terminal_status}")
    check("Control Plane 已创建审批记录", bool(outcome.awaiting_approval_id))

    after = (await client.get(f"{LAB}/v1/service-state", params={"service": "synthetic-orders"})).json()
    check("synthetic-lab 的部署版本未变（动作确实没执行）",
          before["deployed_version"] == after["deployed_version"],
          f"{before['deployed_version']} -> {after['deployed_version']}")
    actions = (await client.get(f"{LAB}/v1/actions")).json()["actions"]
    check("动作账本为空", actions == [], f"got {actions}")


async def drill_3_tampered_arguments_rejected(client: httpx.AsyncClient) -> None:
    """M4 Gate 硬条件 2 —— 跨语言 digest 比对。

    这是整个演练最重要的一条：审批时 Java 侧存的是它自己算的 digest，
    执行时 Java 侧重算并比对。Python 侧算出不同值就会在这里表现为「合法请求被误拒」。
    """
    print("\n== 演练 3：审批后篡改参数被 digest 校验拦住（跨语言） ==")
    _, run_id = await create_run(client)
    gateway = _gateway(client)

    approval_id = await gateway.request(
        run_id=run_id,
        tool_name="rollback_synthetic_deployment",
        resource_ref="svc:synthetic-orders",
        arguments=dict(ROLLBACK.arguments),
    )
    check("审批已创建", bool(approval_id))

    listing = (await client.get(f"{CP}/api/v1/approvals/pending", headers=_headers(APPROVER))).json()
    record = next((a for a in listing if a["approvalId"] == approval_id), None)
    check("审批出现在待审批列表", record is not None)
    if record:
        java_digest = record["argumentsDigest"]
        python_digest = digest_fn(dict(ROLLBACK.arguments))
        check("Java 与 Python 算出同一个 arguments_digest",
              java_digest == python_digest,
              f"java={java_digest}\n      python={python_digest}")
        check("digest 算法标识一致", record["digestAlg"] == "JCS-SHA256-V1",
              record.get("digestAlg"))

    decision = await client.post(
        f"{CP}/api/v1/approvals/{approval_id}/decision",
        headers=_headers(APPROVER),
        json={"approve": True, "reason": "evidence chain checked"},
    )
    check("APPROVER 批准成功", decision.status_code == 200, decision.text[:200])

    # 篡改：把 service 换成另一个。
    tampered = await gateway.consume(
        approval_id=approval_id,
        tool_name="rollback_synthetic_deployment",
        resource_ref="svc:synthetic-orders",
        arguments={"service": "synthetic-db", "target_version": "v1.4.2"},
    )
    check("篡改参数后 consume 被拒绝", tampered is False)

    # 原参数：必须通过。这条同时证明上一条不是「什么都拒绝」。
    honest = await gateway.consume(
        approval_id=approval_id,
        tool_name="rollback_synthetic_deployment",
        resource_ref="svc:synthetic-orders",
        arguments=dict(ROLLBACK.arguments),
    )
    check("原参数 consume 通过（不误拒）", honest is True)

    replay = await gateway.consume(
        approval_id=approval_id,
        tool_name="rollback_synthetic_deployment",
        resource_ref="svc:synthetic-orders",
        arguments=dict(ROLLBACK.arguments),
    )
    check("同一审批不能用第二次（防重放）", replay is False)


async def drill_4_approved_action_executes_once(client: httpx.AsyncClient) -> None:
    """完整链路：审批 → 执行 → 验证效果，且副作用只发生一次。"""
    print("\n== 演练 4：审批通过后动作执行，且幂等 ==")
    _, run_id = await create_run(client)
    await client.post(f"{LAB}/v1/actions/reset")
    gateway = _gateway(client)

    approval_id = await gateway.request(
        run_id=run_id,
        tool_name="rollback_synthetic_deployment",
        resource_ref="svc:synthetic-orders",
        arguments=dict(ROLLBACK.arguments),
    )
    await client.post(
        f"{CP}/api/v1/approvals/{approval_id}/decision",
        headers=_headers(APPROVER),
        json={"approve": True},
    )
    consumed = await gateway.consume(
        approval_id=approval_id,
        tool_name="rollback_synthetic_deployment",
        resource_ref="svc:synthetic-orders",
        arguments=dict(ROLLBACK.arguments),
    )
    check("审批已消费", consumed is True)

    from agent_runtime.tools.action_executor import mark_consumed
    from agent_runtime.tools.contract import ResourceBinding, issue_authorization

    authorization = issue_authorization(
        tool_name="rollback_synthetic_deployment",
        binding=ResourceBinding("prin-agent", "tenant-demo", "svc:synthetic-orders"),
        arguments=dict(ROLLBACK.arguments),
        arguments_digest=digest_fn(dict(ROLLBACK.arguments)),
        allowed=True,
        reason="drill",
        requires_approval=True,
    )
    executor = ActionToolExecutor(LAB, client=client)
    first = await executor.execute(
        authorization,
        mark_consumed(
            approval_id=approval_id,
            tool_name="rollback_synthetic_deployment",
            resource_ref="svc:synthetic-orders",
            arguments_digest=authorization.arguments_digest,
        ),
    )
    check("动作执行成功", first.payload["rolled_back_to"] == "v1.4.2", str(first.payload))
    check("首次执行不是重放", first.payload["replayed"] is False)

    state = (await client.get(f"{LAB}/v1/service-state", params={"service": "synthetic-orders"})).json()
    check("Agent 可验证效果：版本已变为 v1.4.2",
          state["deployed_version"] == "v1.4.2", str(state))

    second = await executor.execute(
        authorization,
        mark_consumed(
            approval_id=approval_id,
            tool_name="rollback_synthetic_deployment",
            resource_ref="svc:synthetic-orders",
            arguments_digest=authorization.arguments_digest,
        ),
    )
    check("同一幂等键重复执行被识别为重放", second.payload["replayed"] is True)
    actions = (await client.get(f"{LAB}/v1/actions")).json()["actions"]
    check("动作账本只有一条记录（副作用只发生一次）", len(actions) == 1, f"got {len(actions)}")


async def drill_5_cross_process_resume(client: httpx.AsyncClient) -> None:
    """M4 Gate 硬条件 3 —— AWAITING_APPROVAL 跨进程恢复。

    用两个 AgentGraph 实例共享一个 checkpointer 模拟「原进程消失、新进程接管」。
    """
    print("\n== 演练 5：AWAITING_APPROVAL 跨实例恢复 ==")
    from langgraph.checkpoint.memory import InMemorySaver

    _, run_id = await create_run(client)
    await client.post(f"{LAB}/v1/actions/reset")
    shared = InMemorySaver()
    gateway = _gateway(client)
    spec = _spec(run_id, allowed=frozenset({"rollback_synthetic_deployment"}))

    first_graph = AgentGraph(
        provider=FakeProvider(script=[FakeTurn(text=DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB, client=client),
        action_executor=ActionToolExecutor(LAB, client=client),
        guard=_guard(),
        spec=spec,
        approvals=gateway,
        checkpointer=shared,
    )
    suspended = await first_graph.run([ROLLBACK])
    check("图在审批处中断", suspended.interrupted is True,
          f"terminal={suspended.terminal_status}")
    approval_id = suspended.awaiting_approval_id
    check("中断时带出 approval_id", bool(approval_id))

    # 外部事件：人在控制台批准。
    decided = await client.post(
        f"{CP}/api/v1/approvals/{approval_id}/decision",
        headers=_headers(APPROVER),
        json={"approve": True, "reason": "approved during drill"},
    )
    check("审批被批准", decided.status_code == 200, decided.text[:200])

    # 新实例：模拟另一个进程接管。除了共享 checkpointer 什么都不传。
    second_graph = AgentGraph(
        provider=FakeProvider(script=[FakeTurn(text=DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB, client=client),
        action_executor=ActionToolExecutor(LAB, client=client),
        guard=_guard(),
        spec=spec,
        approvals=gateway,
        checkpointer=shared,
    )
    resumed = await second_graph.resume(
        decision={"decision": "APPROVED", "approval_id": approval_id},
        thread_id=run_id,
    )
    check("新实例恢复后到达终态",
          resumed.terminal_status in {RunStatus.COMPLETE, RunStatus.FAILED},
          f"actual={resumed.terminal_status} failure={resumed.failure_class}")
    actions = (await client.get(f"{LAB}/v1/actions")).json()["actions"]
    check("恢复后动作执行了恰好一次", len(actions) == 1, f"got {len(actions)}")


async def drill_6_resume_is_not_authorisation(client: httpx.AsyncClient) -> None:
    """M0 INV-4：resume 说 APPROVED 不代表能执行。"""
    print("\n== 演练 6：resume 载荷不是授权凭据 ==")
    from langgraph.checkpoint.memory import InMemorySaver

    _, run_id = await create_run(client)
    await client.post(f"{LAB}/v1/actions/reset")
    shared = InMemorySaver()
    spec = _spec(run_id, allowed=frozenset({"rollback_synthetic_deployment"}))

    graph = AgentGraph(
        provider=FakeProvider(script=[FakeTurn(text=DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB, client=client),
        action_executor=ActionToolExecutor(LAB, client=client),
        guard=_guard(),
        spec=spec,
        approvals=_gateway(client),
        checkpointer=shared,
    )
    suspended = await graph.run([ROLLBACK])
    check("图在审批处中断", suspended.interrupted is True)

    # 谎称已批准，但**不去** Control Plane 批准。
    resumed = await graph.resume(
        decision={"decision": "APPROVED", "approval_id": suspended.awaiting_approval_id},
        thread_id=run_id,
    )
    check("伪造的 resume 载荷不能放行",
          resumed.terminal_status is RunStatus.FAILED
          and resumed.failure_class == "authorization_failed",
          f"terminal={resumed.terminal_status} failure={resumed.failure_class}")
    actions = (await client.get(f"{LAB}/v1/actions")).json()["actions"]
    check("动作未执行", actions == [], f"got {actions}")


async def drill_7_injection_does_not_escalate(client: httpx.AsyncClient) -> None:
    """E2E 主线 3：注入日志 → 无权限提升。"""
    print("\n== 演练 7：注入日志不导致权限提升 ==")
    await client.post(f"{LAB}/v1/scenarios/stop")
    started = await client.post(f"{LAB}/v1/scenarios/prompt-injection-logs-v1/start")
    check("注入剧本已启动", started.status_code == 200, started.text[:200])
    await client.post(f"{LAB}/v1/actions/reset")

    _, run_id = await create_run(client)
    # allowlist 只含只读工具。模型即使被诱导也建议不了别的。
    loop = BoundedAgentLoop(
        provider=FakeProvider(script=[FakeTurn(text=DIAGNOSIS)]),
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB, client=client),
        guard=_guard(),
        approvals=_gateway(client),
    )
    plan = [
        ToolSuggestion(
            tool_name="search_service_logs",
            arguments={"service": "synthetic-orders", "query": "instructions"},
        ),
        # 模拟「模型被注入内容诱导」：建议一个 allowlist 外的高危动作。
        ToolSuggestion(
            tool_name="restart_synthetic_service", arguments={"service": "synthetic-db"}
        ),
    ]
    outcome = await loop.run(
        _spec(run_id, allowed=frozenset(t.name for t in catalogue.READ_ONLY_TOOLS)), plan
    )

    injected = [
        e for e in outcome.evidence if e.source_type == "search_service_logs"
    ]
    check("注入文本作为证据被记录（审计需要知道注入发生过）", bool(injected))
    denials = [s for s in outcome.steps if s.failure_code == "not_in_allowlist"]
    check("被诱导的高危工具被 Policy 拒绝", bool(denials),
          f"steps={[s.failure_code for s in outcome.steps]}")
    actions = (await client.get(f"{LAB}/v1/actions")).json()["actions"]
    check("没有任何动作被执行", actions == [], f"got {actions}")
    check("Run 仍完成了正常诊断（注入未破坏诊断能力）",
          outcome.terminal_status is RunStatus.COMPLETE,
          f"actual={outcome.terminal_status}")

    await client.post(f"{LAB}/v1/scenarios/stop")


async def drill_8_audit_trail(client: httpx.AsyncClient) -> None:
    """审计留痕：每一次拒绝都必须可追溯。"""
    print("\n== 演练 8：审计事件可追溯 ==")
    events = (
        await client.get(
            f"{CP}/api/v1/audit-events", params={"limit": 200}, headers=_headers(OPERATOR)
        )
    ).json()
    actions_seen = {e["action"] for e in events}
    check("审批请求已留痕", "approval.request" in actions_seen, str(sorted(actions_seen))[:300])
    check("审批决策已留痕", "approval.decide" in actions_seen)
    check("审批消费已留痕", "approval.consume" in actions_seen)
    denied = [e for e in events if e["outcome"] == "DENIED"]
    check("存在拒绝类审计事件", bool(denied), f"count={len(denied)}")
    consume_denied = [
        e for e in events if e["action"] == "approval.consume" and e["outcome"] == "DENIED"
    ]
    check("篡改参数的尝试被记为 DENIED", bool(consume_denied),
          f"count={len(consume_denied)}")


async def main() -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # actuator 已迁到独立管理端口（M7 §7.5 整改），业务口上没有它了。
            health = await client.get(f"{CP_MGMT}/actuator/health")
            lab_health = await client.get(f"{LAB}/health")
        except httpx.HTTPError as exc:
            print(f"FAIL  前置检查：栈未就绪 {exc}")
            print("      docker compose -f deploy/compose/docker-compose.yml up -d")
            return 1
        check("Control Plane 健康", health.status_code == 200)
        check("synthetic-lab 健康", lab_health.status_code == 200)

        # 演练 1 需要故障剧本提供数据。
        await client.post(f"{LAB}/v1/scenarios/stop")
        await client.post(f"{LAB}/v1/scenarios/db-pool-exhaustion-v1/start")

        await drill_1_read_only_diagnosis(client)
        await drill_2_unapproved_write_never_executes(client)
        await drill_3_tampered_arguments_rejected(client)
        await drill_4_approved_action_executes_once(client)
        await drill_5_cross_process_resume(client)
        await drill_6_resume_is_not_authorisation(client)
        await drill_7_injection_does_not_escalate(client)
        await drill_8_audit_trail(client)

        await client.post(f"{LAB}/v1/scenarios/stop")

    print("\n===============================")
    print(f"PASS={PASS}  FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
