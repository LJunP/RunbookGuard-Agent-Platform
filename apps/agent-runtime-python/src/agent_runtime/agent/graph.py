"""LangGraph 编排（ADR-0006）。

与 bounded_loop.py 表达同一个状态机，但**复用**它的守卫、Policy 与 Executor。
两条路径的一致性测试（test_graph_parity.py）因此能回答一个具体问题：
行为相同的部分是我自己实现的，只有这一条独有的才是 LangGraph 的贡献
——检查点持久化、interrupt/resume、跨进程恢复。

刻意不用的 LangGraph 设施：
  recursion_limit  —— 它说不出「是 max_steps 还是 deadline」，而 M6 要按 8 种原因归因
  interrupt_before —— 那是无条件暂停的调试设施；审批是条件性的
  LangChain 的 BaseChatModel —— 会把 M3 的类型化异常包成框架异常，failure_class 就丢了
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..provider.base import ChatProvider
from ..provider.errors import ProviderError, StructuredOutputError
from ..tools.catalogue import BY_NAME
from ..tools.contract import ResourceBinding, ToolSuggestion, issue_authorization
from ..tools.executor import ReadOnlyToolExecutor, ToolFailure
from ..tools.policy import PolicyEngine, PolicyInput
from .bounded_loop import (
    ApprovalGateway,
    Evidence,
    RunSpec,
    _build_prompt,
    _identity_of,
    _validate_arguments,
)
from .evidence_summary import summarise
from .state import RunStatus
from .termination import BoundedLoopGuard, TerminationReason, state_fingerprint


def _append(left: list, right: list) -> list:
    """LangGraph 的 reducer：节点返回的列表追加而非覆盖。"""
    return (left or []) + (right or [])


class GraphState(TypedDict, total=False):
    """图状态。

    只放**可序列化**的东西：它要被 checkpointer 写盘并在另一个进程里读回。
    guard / provider / executor 这些活对象通过闭包捕获，不进状态。
    """

    run_id: str
    status: str
    pending: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    steps: Annotated[list[dict[str, Any]], _append]
    diagnosis: dict[str, Any] | None
    failure_class: str | None
    terminal: bool
    awaiting_approval_id: str | None
    round: int
    # 路由提示。必须声明在 schema 里：LangGraph 按 TypedDict 的键建立 channel，
    # 未声明的键会被静默丢弃——节点返回了它，条件边却读不到，于是所有分支都走默认值。
    route: str
    # 正在处理的工具建议。放进状态而非 self：状态会被 checkpointer 写盘，
    # self 上的属性不会——恢复时（尤其在新进程里）self 是空的。
    current: dict[str, Any] | None
    last_result: dict[str, Any] | None


@dataclass
class GraphOutcome:
    terminal_status: RunStatus
    failure_class: str | None
    steps: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    diagnosis: dict[str, Any] | None = None
    awaiting_approval_id: str | None = None
    interrupted: bool = False


class AgentGraph:
    """把 bounded_loop 的逻辑重新表达为 LangGraph 图。

    节点划分与 M0 §6 的状态一一对应，使 Trace 里的节点名就是状态名。
    """

    GRAPH_VERSION = "langgraph-v1"
    STATE_SCHEMA_VERSION = "1"

    def __init__(
        self,
        *,
        provider: ChatProvider,
        policy: PolicyEngine,
        executor: ReadOnlyToolExecutor,
        guard: BoundedLoopGuard,
        spec: RunSpec,
        approvals: ApprovalGateway | None = None,
        action_executor: Any | None = None,
        checkpointer: Any | None = None,
        safety_rule_hit: bool = False,
        version_incompatible: bool = False,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.executor = executor
        self.action_executor = action_executor
        self.guard = guard
        self.spec = spec
        self.approvals = approvals
        self.safety_rule_hit = safety_rule_hit
        self.version_incompatible = version_incompatible
        from ..approval.digest import digest as _digest

        self._digest = _digest
        self._checkpointer = checkpointer or InMemorySaver()
        self._compiled = self._build().compile(checkpointer=self._checkpointer)

    # -- 图构造 -----------------------------------------------------------

    def _build(self) -> StateGraph:
        graph = StateGraph(GraphState)
        graph.add_node("COLLECT_CONTEXT", self._collect_context)
        graph.add_node("RETRIEVE_RUNBOOK", self._retrieve_runbook)
        graph.add_node("FORM_HYPOTHESES", self._form_hypotheses)
        graph.add_node("SELECT_TOOL", self._select_tool)
        graph.add_node("POLICY_CHECK", self._policy_check)
        graph.add_node("AWAITING_APPROVAL", self._awaiting_approval)
        graph.add_node("EXECUTING_TOOL", self._executing_tool)
        graph.add_node("OBSERVE", self._observe)
        graph.add_node("VERIFY", self._verify)
        graph.add_node("PROPOSE_ACTION", self._propose_action)

        graph.add_edge(START, "COLLECT_CONTEXT")
        graph.add_conditional_edges(
            "COLLECT_CONTEXT", self._after_step, {"stop": END, "next": "RETRIEVE_RUNBOOK"}
        )
        graph.add_conditional_edges(
            "RETRIEVE_RUNBOOK", self._after_step, {"stop": END, "next": "FORM_HYPOTHESES"}
        )
        graph.add_conditional_edges(
            "FORM_HYPOTHESES",
            self._route_from_hypotheses,
            {"stop": END, "select": "SELECT_TOOL", "conclude": "PROPOSE_ACTION"},
        )
        graph.add_conditional_edges(
            "SELECT_TOOL", self._after_step, {"stop": END, "next": "POLICY_CHECK"}
        )
        graph.add_conditional_edges(
            "POLICY_CHECK",
            self._route_from_policy,
            {
                "stop": END,
                "await": "AWAITING_APPROVAL",
                "execute": "EXECUTING_TOOL",
                "next_tool": "SELECT_TOOL",
                "conclude": "PROPOSE_ACTION",
            },
        )
        graph.add_conditional_edges(
            "AWAITING_APPROVAL",
            self._route_from_approval,
            {"stop": END, "execute": "EXECUTING_TOOL"},
        )
        graph.add_conditional_edges(
            "EXECUTING_TOOL", self._after_step, {"stop": END, "next": "OBSERVE"}
        )
        graph.add_conditional_edges("OBSERVE", self._after_step, {"stop": END, "next": "VERIFY"})
        graph.add_conditional_edges(
            "VERIFY",
            self._route_from_verify,
            {"stop": END, "select": "SELECT_TOOL", "conclude": "PROPOSE_ACTION"},
        )
        graph.add_edge("PROPOSE_ACTION", END)
        return graph

    # -- 节点 -------------------------------------------------------------

    def _collect_context(self, state: GraphState) -> dict:
        return self._step(RunStatus.COLLECT_CONTEXT, state)

    def _retrieve_runbook(self, state: GraphState) -> dict:
        return self._step(RunStatus.RETRIEVE_RUNBOOK, state)

    def _form_hypotheses(self, state: GraphState) -> dict:
        return self._step(RunStatus.FORM_HYPOTHESES, state)

    def _select_tool(self, state: GraphState) -> dict:
        pending = state.get("pending") or []
        tool_name = pending[0]["tool_name"] if pending else None
        fingerprint = state_fingerprint(
            RunStatus.SELECT_TOOL.value,
            [e["evidence_id"] for e in state.get("evidence", [])],
            tool_name,
        )
        self.guard.repeated_state.observe(fingerprint)
        update = self._step(RunStatus.SELECT_TOOL, state, detail=tool_name or "")
        if update.get("terminal"):
            return update
        stop = self._termination(fingerprint=fingerprint)
        if stop:
            return self._terminal_update(RunStatus.SELECT_TOOL, *stop)
        update["round"] = state.get("round", 0) + 1
        return update

    async def _policy_check(self, state: GraphState) -> dict:
        pending = list(state.get("pending") or [])
        if not pending:
            return {"status": RunStatus.POLICY_CHECK.value, "route": "conclude"}
        raw = pending.pop(0)
        suggestion = ToolSuggestion(tool_name=raw["tool_name"], arguments=dict(raw["arguments"]))

        contract = BY_NAME.get(suggestion.tool_name)
        binding = ResourceBinding(
            principal_id=self.spec.principal_id,
            tenant_id=self.spec.tenant_id,
            resource_ref=_resource_ref(suggestion),
        )
        try:
            digest = self._digest(suggestion.arguments)
        except Exception:
            digest = None

        approval_id = None
        approval_fact = None
        if contract is not None and contract.requires_approval and self.approvals is not None:
            approval_id = await self.approvals.request(
                run_id=self.spec.run_id,
                tool_name=suggestion.tool_name,
                resource_ref=binding.resource_ref,
                arguments=suggestion.arguments,
            )
            approval_fact = await self.approvals.fetch(approval_id=approval_id)

        decision = self.policy.decide(
            PolicyInput(
                suggestion=suggestion,
                contract=contract,
                binding=binding,
                environment=self.spec.environment,
                allowed_tool_names=self.spec.allowed_tool_names,
                permitted_resources=self.spec.permitted_resources,
                arguments_digest=digest,
                schema_errors=_validate_arguments(contract, suggestion.arguments),
                approval=approval_fact,
                tool_calls_used=self.guard.budget.tool_calls_used,
                tool_call_budget=self.guard.budget.tool_call_budget,
                read_only_run=self.spec.read_only_run,
            )
        )
        self.guard.charge_step()

        step = _step_record(len(state.get("steps", [])) + 1, RunStatus.POLICY_CHECK,
                            detail=decision.reason, tool_name=suggestion.tool_name,
                            failure_code=decision.deny_reason.value if decision.deny_reason else None)
        base: dict[str, Any] = {
            "status": RunStatus.POLICY_CHECK.value,
            "pending": pending,
            "steps": [step],
        }

        if decision.allowed:
            base["current"] = {
                "tool_name": suggestion.tool_name,
                "arguments": dict(suggestion.arguments),
                "resource_ref": binding.resource_ref,
                "arguments_digest": digest,
                "approval_id": approval_id,
            }
            base["route"] = "await" if decision.requires_approval else "execute"
            base["awaiting_approval_id"] = approval_id
            return base

        reason = decision.deny_reason.value if decision.deny_reason else ""
        if reason == "approval_pending":
            base["current"] = {
                "tool_name": suggestion.tool_name,
                "arguments": dict(suggestion.arguments),
                "resource_ref": binding.resource_ref,
                "arguments_digest": digest,
                "approval_id": approval_id,
            }
            base["route"] = "await"
            base["awaiting_approval_id"] = approval_id
            return base
        if reason == "tool_call_budget_exhausted":
            base.update(
                self._terminal_update(
                    RunStatus.POLICY_CHECK,
                    TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED,
                    decision.reason,
                )
            )
            return base
        # 拒绝不终止整个 Run：继续用其它证据推进（S6 的注入 case 依赖这一点）。
        base["route"] = "next_tool" if pending else "conclude"
        return base

    def _awaiting_approval(self, state: GraphState) -> dict:
        """LangGraph 的 interrupt 在这里。

        这是与手写循环唯一的**行为**差异：手写循环返回一个 AWAITING_APPROVAL 结果
        然后结束进程，恢复需要重新构造整个循环；这里 interrupt 让图暂停并把状态交给
        checkpointer，恢复时用 Command(resume=...) 从这个节点继续。

        resume 里携带的 decision **不是授权凭据**（M0 INV-4）：它只是「外部说审批有
        结果了」这一事件。真正的放行仍在 EXECUTING_TOOL 之前调 Java 的 consume。
        """
        current = state.get("current") or {}
        approval_id = state.get("awaiting_approval_id") or current.get("approval_id")
        decision = interrupt(
            {
                "kind": "approval_required",
                "approval_id": approval_id,
                "tool_name": current.get("tool_name"),
                "run_id": self.spec.run_id,
            }
        )
        payload = decision if isinstance(decision, dict) else {}
        return {
            "status": RunStatus.AWAITING_APPROVAL.value,
            "awaiting_approval_id": approval_id,
            "steps": [
                _step_record(
                    len(state.get("steps", [])) + 1,
                    RunStatus.AWAITING_APPROVAL,
                    detail=f"resumed with {payload.get('decision', 'unknown')}",
                )
            ],
        }

    async def _executing_tool(self, state: GraphState) -> dict:
        current = state.get("current") or {}
        if not current:
            # 状态里没有待执行的建议，说明路由有误或 checkpoint 不完整。
            # 不猜一个来执行——那可能执行任意工具。
            return self._terminal_update(
                RunStatus.EXECUTING_TOOL,
                TerminationReason.VERSION_INCOMPATIBLE,
                "checkpoint has no pending tool suggestion; refusing to guess",
            )
        suggestion = ToolSuggestion(
            tool_name=current["tool_name"], arguments=dict(current["arguments"])
        )
        approval_id = current.get("approval_id")
        contract = BY_NAME[suggestion.tool_name]

        if contract.requires_approval:
            if self.approvals is None or approval_id is None:
                return self._terminal_update(
                    RunStatus.EXECUTING_TOOL,
                    TerminationReason.AUTHORIZATION_FAILED,
                    f"{contract.name} requires approval but no gateway is configured",
                )
            # 权威判定在 Java 侧。resume 说了什么不影响这一步的结果。
            allowed = await self.approvals.consume(
                approval_id=approval_id,
                tool_name=suggestion.tool_name,
                resource_ref=current["resource_ref"],
                arguments=suggestion.arguments,
            )
            if not allowed:
                return self._terminal_update(
                    RunStatus.EXECUTING_TOOL,
                    TerminationReason.AUTHORIZATION_FAILED,
                    f"control plane refused to consume approval {approval_id}",
                )

        # 授权在这里重建，而不是从 checkpoint 里读回来。
        # ToolAuthorization 携带 allowed=True，把它序列化进状态等于把「放行凭据」写盘，
        # 任何能改 checkpoint 的人就能伪造放行（issue_authorization 的守卫会被绕过）。
        authorization = issue_authorization(
            tool_name=suggestion.tool_name,
            binding=ResourceBinding(
                principal_id=self.spec.principal_id,
                tenant_id=self.spec.tenant_id,
                resource_ref=current["resource_ref"],
            ),
            arguments=dict(suggestion.arguments),
            arguments_digest=current["arguments_digest"],
            allowed=True,
            reason="re-issued after policy approval within this node",
            requires_approval=contract.requires_approval,
            approval_id=approval_id,
        )

        self.guard.charge_tool_call()
        self.guard.charge_step()
        try:
            if contract.is_write():
                if self.action_executor is None:
                    return self._terminal_update(
                        RunStatus.EXECUTING_TOOL,
                        TerminationReason.AUTHORIZATION_FAILED,
                        f"{contract.name} is a write tool but no action executor is configured",
                    )
                # ConsumedApproval 只能在 consume 成功后构造，因此「未消费审批就执行动作」
                # 在类型层面不成立。
                from ..tools.action_executor import mark_consumed

                result = await self.action_executor.execute(
                    authorization,
                    mark_consumed(
                        approval_id=approval_id,
                        tool_name=contract.name,
                        resource_ref=current["resource_ref"],
                        arguments_digest=current["arguments_digest"],
                    ),
                )
            else:
                result = await self.executor.execute(authorization)
        except ToolFailure as failure:
            failure.validate_against(contract)
            return {
                "status": RunStatus.EXECUTING_TOOL.value,
                "steps": [
                    _step_record(
                        len(state.get("steps", [])) + 1,
                        RunStatus.EXECUTING_TOOL,
                        detail=str(failure),
                        tool_name=suggestion.tool_name,
                        failure_code=failure.failure_code,
                    )
                ],
            }
        return {
            "status": RunStatus.EXECUTING_TOOL.value,
            "last_result": {
                "tool_name": result.tool_name,
                "payload": result.payload,
                "untrusted": result.untrusted,
            },
            "steps": [
                _step_record(
                    len(state.get("steps", [])) + 1,
                    RunStatus.EXECUTING_TOOL,
                    detail=suggestion.tool_name,
                    tool_name=suggestion.tool_name,
                )
            ],
        }

    def _observe(self, state: GraphState) -> dict:
        update = self._step(RunStatus.OBSERVE, state)
        result = state.get("last_result")
        if not result:
            return update

        existing = list(state.get("evidence", []))
        seen = {e["evidence_id"] for e in existing}
        # 与 bounded_loop._absorb 保持同一套规则：检索结果逐段落展开，
        # 未命中不产生证据。两条路径的证据集合必须一致，否则一致性测试会分叉。
        added = [e for e in _evidence_from_result(result) if e["evidence_id"] not in seen]
        if not added:
            return update
        update["evidence"] = existing + added
        return update

    def _verify(self, state: GraphState) -> dict:
        return self._step(RunStatus.VERIFY, state)

    async def _propose_action(self, state: GraphState) -> dict:
        evidence = state.get("evidence", [])
        if not evidence:
            return {
                "status": RunStatus.COMPLETE.value,
                "terminal": True,
                "diagnosis": {
                    "conclusion_type": "insufficient_evidence",
                    "root_cause": "not determined: no tool produced usable evidence",
                    "confidence": "none",
                    "root_cause_service": None,
                    "claims": [],
                    "ruled_out": [],
                    "conflicting_signals": [],
                    "missing_evidence": ["no tool produced usable evidence"],
                    "proposed_action": None,
                    "recommended_next_step": "widen the evidence window or escalate to a human",
                },
                "steps": [
                    _step_record(len(state.get("steps", [])) + 1, RunStatus.PROPOSE_ACTION,
                                 detail="insufficient evidence")
                ],
            }

        from ..provider.structured import parse_structured
        from ..schemas import Diagnosis

        typed = [
            Evidence(
                evidence_id=e["evidence_id"],
                source_type=e["source_type"],
                source_identity=e["source_identity"],
                content_hash=e["content_hash"],
                untrusted=e["untrusted"],
                summary=e.get("summary", ""),
                document_id=e.get("document_id"),
                document_version=e.get("document_version"),
                section_id=e.get("section_id"),
                relevance=e.get("relevance"),
            )
            for e in evidence
        ]
        try:
            completion = await self.provider.complete(_build_prompt(self.spec, typed))
            self.guard.charge_model(
                tokens=completion.usage.total_tokens, cost_micros=completion.cost_micros
            )
            stop = self._termination()
            if stop:
                return self._terminal_update(RunStatus.PROPOSE_ACTION, *stop)
            parsed = parse_structured(completion.text, Diagnosis)
        except (ProviderError, StructuredOutputError) as exc:
            return self._terminal_update(
                RunStatus.PROPOSE_ACTION,
                None,
                str(exc),
                explicit_failure_class=getattr(exc, "failure_class", "provider_failure"),
            )

        payload = parsed.value.model_dump(mode="json")
        payload["unwrapped"] = parsed.unwrapped
        return {
            "status": RunStatus.COMPLETE.value,
            "terminal": True,
            "diagnosis": payload,
            "steps": [
                _step_record(len(state.get("steps", [])) + 1, RunStatus.PROPOSE_ACTION,
                             detail="diagnosis produced")
            ],
        }

    # -- 路由 -------------------------------------------------------------

    def _after_step(self, state: GraphState) -> str:
        return "stop" if state.get("terminal") else "next"

    def _route_from_hypotheses(self, state: GraphState) -> str:
        if state.get("terminal"):
            return "stop"
        return "select" if state.get("pending") else "conclude"

    def _route_from_policy(self, state: GraphState) -> str:
        if state.get("terminal"):
            return "stop"
        return state.get("route", "conclude")

    def _route_from_approval(self, state: GraphState) -> str:
        return "stop" if state.get("terminal") else "execute"

    def _route_from_verify(self, state: GraphState) -> str:
        if state.get("terminal"):
            return "stop"
        if state.get("pending") and state.get("round", 0) < self.spec.max_tool_rounds:
            return "select"
        return "conclude"

    # -- 公共辅助 ---------------------------------------------------------

    def _step(self, node: RunStatus, state: GraphState, *, detail: str = "") -> dict:
        self.guard.charge_step()
        stop = self._termination()
        if stop:
            return self._terminal_update(node, *stop)
        return {
            "status": node.value,
            "steps": [_step_record(len(state.get("steps", [])) + 1, node, detail=detail)],
        }

    def _termination(self, *, fingerprint: str | None = None):
        verdict = self.guard.check(
            fingerprint=fingerprint,
            version_incompatible=self.version_incompatible,
            safety_rule_hit=self.safety_rule_hit,
        )
        return (verdict.reason, verdict.detail) if verdict.should_stop else None

    def _terminal_update(
        self,
        node: RunStatus,
        reason: TerminationReason | None,
        detail: str,
        *,
        explicit_failure_class: str | None = None,
    ) -> dict:
        failure_class = explicit_failure_class or (reason.value if reason else "internal_error")
        return {
            "status": RunStatus.FAILED.value,
            "terminal": True,
            "failure_class": failure_class,
            "steps": [
                _step_record(0, RunStatus.FAILED, detail=detail, failure_code=failure_class)
            ],
        }

    # -- 执行 -------------------------------------------------------------

    async def run(self, plan: list[ToolSuggestion], *, thread_id: str | None = None) -> GraphOutcome:
        config = {"configurable": {"thread_id": thread_id or self.spec.run_id}}
        initial: GraphState = {
            "run_id": self.spec.run_id,
            "status": RunStatus.CREATED.value,
            "pending": [{"tool_name": s.tool_name, "arguments": dict(s.arguments)} for s in plan],
            "evidence": [],
            "steps": [],
            "diagnosis": None,
            "failure_class": None,
            "terminal": False,
            "awaiting_approval_id": None,
            "round": 0,
        }
        final = await self._compiled.ainvoke(initial, config)
        return self._to_outcome(final, config)

    async def resume(self, *, decision: dict[str, Any], thread_id: str) -> GraphOutcome:
        """从 interrupt 处恢复。

        decision 只是事件通知，不是授权——EXECUTING_TOOL 仍会调 Java 的 consume。
        """
        from langgraph.types import Command

        config = {"configurable": {"thread_id": thread_id}}
        final = await self._compiled.ainvoke(Command(resume=decision), config)
        return self._to_outcome(final, config)

    def _to_outcome(self, final: dict, config: dict) -> GraphOutcome:
        snapshot = self._compiled.get_state(config)
        interrupted = bool(getattr(snapshot, "next", ()) )
        status_value = final.get("status", RunStatus.FAILED.value)
        if interrupted:
            terminal = RunStatus.AWAITING_APPROVAL
        elif final.get("failure_class"):
            terminal = RunStatus.FAILED
        elif status_value == RunStatus.COMPLETE.value:
            terminal = RunStatus.COMPLETE
        else:
            terminal = RunStatus(status_value)
        return GraphOutcome(
            terminal_status=terminal,
            failure_class=final.get("failure_class"),
            steps=final.get("steps", []),
            evidence=final.get("evidence", []),
            diagnosis=final.get("diagnosis"),
            awaiting_approval_id=final.get("awaiting_approval_id"),
            interrupted=interrupted,
        )


def _step_record(
    sequence: int,
    node: RunStatus,
    *,
    detail: str = "",
    tool_name: str | None = None,
    failure_code: str | None = None,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "node": node.value,
        "detail": detail,
        "tool_name": tool_name,
        "failure_code": failure_code,
    }


def _resource_ref(suggestion: ToolSuggestion) -> str:
    args = suggestion.arguments
    if "service" in args:
        return f"svc:{args['service']}"
    if "queue" in args:
        return f"queue:{args['queue']}"
    return "runbook:*"


def _evidence_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    """把工具结果转成证据条目。

    与 bounded_loop._absorb 同一套规则。刻意重复实现而非共享：graph 的证据是
    可序列化的 dict（要进 checkpoint），bounded_loop 用的是 dataclass。
    两者的一致性由 test_graph_parity 保证。
    """
    import hashlib
    import json as _json

    payload = result.get("payload", {})
    tool_name = result.get("tool_name", "")

    if tool_name == "retrieve_runbook_section":
        # 未命中不产生证据：「查了但没有」不是一份可用证据。
        if not payload.get("retrieval_hit"):
            return []
        out = []
        for section in payload.get("sections", []):
            section_id = section.get("section_id")
            if not section_id:
                continue
            out.append(
                {
                    "evidence_id": f"ev-{section.get('content_hash', '')[:12]}",
                    "source_type": tool_name,
                    "source_identity": section_id,
                    "content_hash": section.get("content_hash", ""),
                    "untrusted": bool(section.get("untrusted", True)),
                    "summary": summarise(
                        tool_name, {"retrieval_hit": True, "sections": [section]}
                    ),
                    "document_id": section.get("document_id"),
                    "document_version": section.get("document_version"),
                    "section_id": section_id,
                    "relevance": section.get("relevance"),
                }
            )
        return out

    raw = _json.dumps(payload, sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(raw.encode()).hexdigest()
    return [
        {
            "evidence_id": f"ev-{content_hash[:12]}",
            "source_type": tool_name,
            "source_identity": _identity_from_payload(result),
            "content_hash": content_hash,
            "untrusted": result.get("untrusted", True),
            "summary": summarise(tool_name, payload),
            "document_id": None,
            "document_version": None,
            "section_id": None,
            "relevance": None,
        }
    ]


def _identity_from_payload(result: dict[str, Any]) -> str:
    payload = result.get("payload", {})
    for key in ("service", "queue"):
        if key in payload:
            return f"{key}:{payload[key]}"
    return result.get("tool_name", "unknown")
