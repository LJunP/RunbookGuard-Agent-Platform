"""Policy 引擎与工具契约的测试。

M4 Gate 的硬条件是「未审批的写动作 100% 不执行」，因此这里的重点是拒绝路径，
以及「模型建议无法自己变成授权」这条类型层面的保证。
"""

from __future__ import annotations

import pytest

from agent_runtime.tools import catalogue
from agent_runtime.tools.contract import (
    CancelSemantics,
    ResourceBinding,
    RetryPolicy,
    RiskLevel,
    ToolAuthorization,
    ToolContract,
    ToolEnvironment,
    ToolSuggestion,
    issue_authorization,
)
from agent_runtime.tools.policy import (
    ApprovalFact,
    DenyReason,
    PolicyEngine,
    PolicyInput,
)

ENGINE = PolicyEngine()
LAB = ToolEnvironment.SYNTHETIC_LAB
BINDING = ResourceBinding(
    principal_id="prin-agent", tenant_id="tenant-demo", resource_ref="svc:synthetic-orders"
)
PERMITTED = frozenset({"svc:synthetic-orders", "queue:synthetic-notify.work"})
ALL_NAMES = frozenset(catalogue.BY_NAME)
READ_NAMES = frozenset(t.name for t in catalogue.READ_ONLY_TOOLS)
DIGEST = "a" * 64


def _input(**overrides) -> PolicyInput:
    defaults = dict(
        suggestion=ToolSuggestion(tool_name="get_service_metrics",
                                  arguments={"service": "synthetic-orders", "metric": "db_pool_active"}),
        contract=catalogue.GET_SERVICE_METRICS,
        binding=BINDING,
        environment=LAB,
        allowed_tool_names=ALL_NAMES,
        permitted_resources=PERMITTED,
        arguments_digest=DIGEST,
        tool_call_budget=20,
    )
    defaults.update(overrides)
    return PolicyInput(**defaults)


def _approval(**overrides) -> ApprovalFact:
    defaults = dict(
        approval_id="apr-1",
        tool_name="rollback_synthetic_deployment",
        resource_ref="svc:synthetic-orders",
        arguments_digest=DIGEST,
        decision="APPROVED",
        consumed=False,
    )
    defaults.update(overrides)
    return ApprovalFact(**defaults)


def _write_input(**overrides) -> PolicyInput:
    base = dict(
        suggestion=ToolSuggestion(
            tool_name="rollback_synthetic_deployment",
            arguments={"service": "synthetic-orders", "target_version": "v1.4.2"},
        ),
        contract=catalogue.ROLLBACK_SYNTHETIC_DEPLOYMENT,
        approval=_approval(),
    )
    base.update(overrides)
    return _input(**base)


# --------------------------------------------------------------------------
# 契约完整性
# --------------------------------------------------------------------------

class TestContractCompleteness:
    def test_five_read_only_tools(self) -> None:
        """说明书 §14 固定 5 个只读工具。运行时事件并入 get_service_metrics，
        不新增第 6 个（ADR-0001 C-5 的折中）。"""
        assert len(catalogue.READ_ONLY_TOOLS) == 5
        assert {t.name for t in catalogue.READ_ONLY_TOOLS} == {
            "get_service_metrics",
            "search_service_logs",
            "get_recent_deployments",
            "get_queue_state",
            "retrieve_runbook_section",
        }

    def test_three_action_tools(self) -> None:
        assert len(catalogue.ACTION_TOOLS) == 3
        assert {t.name for t in catalogue.ACTION_TOOLS} == {
            "restart_synthetic_service",
            "rollback_synthetic_deployment",
            "throttle_synthetic_traffic",
        }

    @pytest.mark.parametrize("tool", catalogue.ALL_TOOLS, ids=lambda t: t.name)
    def test_every_tool_declares_all_eleven_items(self, tool: ToolContract) -> None:
        assert tool.name and tool.version and tool.description
        assert tool.input_schema and tool.output_schema
        assert tool.resource_kind
        assert isinstance(tool.risk, RiskLevel)
        assert tool.timeout_seconds > 0 and tool.max_result_bytes > 0
        assert isinstance(tool.cancel_semantics, CancelSemantics)
        assert tool.retry.max_attempts >= 1
        assert tool.audit_fields
        assert tool.allowed_environments
        assert tool.typed_failures
        assert isinstance(tool.requires_approval, bool)

    @pytest.mark.parametrize("tool", catalogue.ALL_TOOLS, ids=lambda t: t.name)
    def test_only_synthetic_lab_is_allowed(self, tool: ToolContract) -> None:
        assert tool.allowed_environments == frozenset({ToolEnvironment.SYNTHETIC_LAB})

    @pytest.mark.parametrize("tool", catalogue.ACTION_TOOLS, ids=lambda t: t.name)
    def test_action_tools_require_approval_and_idempotency(self, tool: ToolContract) -> None:
        assert tool.requires_approval is True
        assert tool.idempotency_key_template
        assert tool.cancel_semantics is CancelSemantics.FORCEFUL

    @pytest.mark.parametrize("tool", catalogue.READ_ONLY_TOOLS, ids=lambda t: t.name)
    def test_read_tools_do_not_require_approval(self, tool: ToolContract) -> None:
        """只读工具要审批就等于取证需要人工签字，Agent 无法工作。"""
        assert tool.requires_approval is False
        assert tool.idempotency_key_template is None

    def test_write_tool_without_approval_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="must require approval"):
            ToolContract(
                name="bad_write", version="1.0.0", description="x",
                input_schema={}, output_schema={},
                requires_principal=True, requires_tenant=True, resource_kind="service",
                risk=RiskLevel.WRITE, timeout_seconds=1.0,
                cancel_semantics=CancelSemantics.FORCEFUL, max_result_bytes=1024,
                retry=RetryPolicy(1, (), frozenset()),
                idempotency_key_template="x", audit_fields=("a",),
                allowed_environments=frozenset({ToolEnvironment.SYNTHETIC_LAB}),
                typed_failures=frozenset({"f"}), requires_approval=False,
            )

    def test_write_tool_without_idempotency_key_is_rejected(self) -> None:
        """ADR-0003 的推论：自身不幂等的动作工具不允许进入工具集。"""
        with pytest.raises(ValueError, match="idempotency key"):
            ToolContract(
                name="bad_write2", version="1.0.0", description="x",
                input_schema={}, output_schema={},
                requires_principal=True, requires_tenant=True, resource_kind="service",
                risk=RiskLevel.WRITE, timeout_seconds=1.0,
                cancel_semantics=CancelSemantics.FORCEFUL, max_result_bytes=1024,
                retry=RetryPolicy(1, (), frozenset()),
                idempotency_key_template=None, audit_fields=("a",),
                allowed_environments=frozenset({ToolEnvironment.SYNTHETIC_LAB}),
                typed_failures=frozenset({"f"}), requires_approval=True,
            )

    def test_empty_typed_failures_is_rejected(self) -> None:
        """未分类的失败会让 M6 的归因统计出现吞掉一切的黑洞。"""
        with pytest.raises(ValueError, match="typed_failures"):
            ToolContract(
                name="no_failures", version="1.0.0", description="x",
                input_schema={}, output_schema={},
                requires_principal=True, requires_tenant=True, resource_kind="service",
                risk=RiskLevel.READ, timeout_seconds=1.0,
                cancel_semantics=CancelSemantics.COOPERATIVE, max_result_bytes=1024,
                retry=RetryPolicy(1, (), frozenset()),
                idempotency_key_template=None, audit_fields=("a",),
                allowed_environments=frozenset({ToolEnvironment.SYNTHETIC_LAB}),
                typed_failures=frozenset(), requires_approval=False,
            )

    def test_deployments_schema_forbids_extra_fields(self) -> None:
        """synthetic-lab 返回 is_decoy（grader 用的标记），透传给 Agent
        就等于把答案交给被试（M2.5 Gate 报告 §5.1 第 5 条）。"""
        item_schema = catalogue.GET_RECENT_DEPLOYMENTS.output_schema["properties"]["deployments"]["items"]
        assert item_schema["additionalProperties"] is False
        assert "is_decoy" not in item_schema["properties"]

    def test_throttle_uses_integer_basis_points_not_float(self) -> None:
        prop = catalogue.THROTTLE_SYNTHETIC_TRAFFIC.input_schema["properties"]["rate_basis_points"]
        assert prop["type"] == "integer"
        assert prop["maximum"] == 10000

    def test_log_entries_are_marked_untrusted(self) -> None:
        props = catalogue.SEARCH_SERVICE_LOGS.output_schema["properties"]["entries"]["items"]
        assert "untrusted" in props["required"]

    def test_runbook_retrieval_exposes_miss_explicitly(self) -> None:
        """静默返回低相关结果会让 S7（Runbook 缺失）的正确弃答无法判定。"""
        assert "retrieval_hit" in catalogue.RETRIEVE_RUNBOOK_SECTION.output_schema["required"]


# --------------------------------------------------------------------------
# 三段分离的类型保证
# --------------------------------------------------------------------------

class TestThreeStageSeparation:
    def test_suggestion_carries_no_authorization(self) -> None:
        s = ToolSuggestion(tool_name="restart_synthetic_service", arguments={})
        assert not hasattr(s, "allowed")
        assert not hasattr(s, "authorization")

    def test_authorization_cannot_be_forged(self) -> None:
        """绕过 PolicyEngine 直接构造 allowed=True 必须失败。
        这让「谁有权放行」在类型层面就是唯一的。"""
        with pytest.raises(PermissionError, match="only be issued by PolicyEngine"):
            ToolAuthorization(
                tool_name="restart_synthetic_service",
                binding=BINDING,
                arguments={},
                arguments_digest=DIGEST,
                allowed=True,
                reason="I said so",
                requires_approval=False,
            )

    def test_denied_authorization_can_be_constructed_freely(self) -> None:
        denied = ToolAuthorization(
            tool_name="x", binding=BINDING, arguments={}, arguments_digest=DIGEST,
            allowed=False, reason="denied", requires_approval=False,
        )
        assert denied.allowed is False

    def test_issue_authorization_produces_valid_allowed(self) -> None:
        auth = issue_authorization(
            tool_name="get_service_metrics", binding=BINDING, arguments={},
            arguments_digest=DIGEST, allowed=True, reason="ok", requires_approval=False,
        )
        assert auth.allowed is True


# --------------------------------------------------------------------------
# Policy：默认拒绝与各条规则
# --------------------------------------------------------------------------

class TestPolicyDenies:
    def test_unknown_tool(self) -> None:
        d = ENGINE.decide(_input(contract=None,
                                 suggestion=ToolSuggestion(tool_name="rm_minus_rf", arguments={})))
        assert d.allowed is False
        assert d.deny_reason is DenyReason.UNKNOWN_TOOL
        assert d.authorization is None

    def test_tool_not_in_allowlist(self) -> None:
        d = ENGINE.decide(_input(allowed_tool_names=frozenset({"search_service_logs"})))
        assert d.deny_reason is DenyReason.NOT_IN_ALLOWLIST

    def test_empty_allowlist_denies_everything(self) -> None:
        """默认拒绝：新增工具若忘记加入 allowlist，表现为不可用而不是可用。"""
        d = ENGINE.decide(_input(allowed_tool_names=frozenset()))
        assert d.allowed is False

    def test_missing_binding_is_rejected_at_construction(self) -> None:
        """binding 的完整性在构造期强制，Policy 拿到的 binding 必然非空。
        空 tenant 能构造出来的话，威胁 T-4 的拦截就只剩运行时检查一道。"""
        with pytest.raises(ValueError, match="tenant_id"):
            ResourceBinding(principal_id="p", tenant_id="", resource_ref="r")
        with pytest.raises(ValueError, match="principal_id"):
            ResourceBinding(principal_id="", tenant_id="t", resource_ref="r")
        with pytest.raises(ValueError, match="resource_ref"):
            ResourceBinding(principal_id="p", tenant_id="t", resource_ref="")

    def test_resource_not_permitted(self) -> None:
        """跨资源访问（威胁 T-4）。许可集合由 Control Plane 派生。"""
        foreign = ResourceBinding(
            principal_id="prin-agent", tenant_id="tenant-demo", resource_ref="svc:synthetic-db"
        )
        d = ENGINE.decide(_input(binding=foreign))
        assert d.deny_reason is DenyReason.RESOURCE_NOT_PERMITTED

    def test_schema_violation(self) -> None:
        d = ENGINE.decide(_input(schema_errors=("service: required field missing",)))
        assert d.deny_reason is DenyReason.SCHEMA_VIOLATION

    def test_non_canonicalizable_arguments(self) -> None:
        """参数无法规范化就无法产生摘要，审批也就无从绑定。"""
        d = ENGINE.decide(_input(arguments_digest=None))
        assert d.deny_reason is DenyReason.NON_CANONICALIZABLE_ARGUMENTS

    def test_tool_call_budget_exhausted(self) -> None:
        d = ENGINE.decide(_input(tool_calls_used=20, tool_call_budget=20))
        assert d.deny_reason is DenyReason.TOOL_CALL_BUDGET_EXHAUSTED

    def test_write_action_in_read_only_run(self) -> None:
        d = ENGINE.decide(_write_input(read_only_run=True))
        assert d.deny_reason is DenyReason.WRITE_ACTION_IN_READ_ONLY_RUN


# --------------------------------------------------------------------------
# Policy：审批（M4 Gate 硬条件）
# --------------------------------------------------------------------------

class TestApprovalEnforcement:
    def test_write_without_approval_is_denied(self) -> None:
        d = ENGINE.decide(_write_input(approval=None))
        assert d.allowed is False
        assert d.deny_reason is DenyReason.APPROVAL_REQUIRED_BUT_ABSENT

    def test_pending_approval_is_denied_but_distinguishable(self) -> None:
        """PENDING 与 REJECTED 是不同的处境：前者应挂起等人，后者应放弃。
        合成一个 reason 会让调用方无法区分「等一下」和「别想了」。"""
        d = ENGINE.decide(_write_input(approval=_approval(decision="PENDING")))
        assert d.allowed is False
        assert d.deny_reason is DenyReason.APPROVAL_PENDING

    def test_rejected_approval_is_denied(self) -> None:
        d = ENGINE.decide(_write_input(approval=_approval(decision="REJECTED")))
        assert d.deny_reason is DenyReason.APPROVAL_NOT_GRANTED

    def test_expired_approval_is_denied(self) -> None:
        d = ENGINE.decide(_write_input(approval=_approval(decision="EXPIRED")))
        assert d.deny_reason is DenyReason.APPROVAL_NOT_GRANTED

    def test_already_consumed_approval_is_denied(self) -> None:
        """防重放：同一份审批不能用两次。"""
        d = ENGINE.decide(_write_input(approval=_approval(consumed=True)))
        assert d.deny_reason is DenyReason.APPROVAL_NOT_GRANTED

    def test_approval_for_a_different_tool_is_denied(self) -> None:
        d = ENGINE.decide(_write_input(approval=_approval(tool_name="restart_synthetic_service")))
        assert d.deny_reason is DenyReason.APPROVAL_TOOL_MISMATCH

    def test_approval_for_a_different_resource_is_denied(self) -> None:
        d = ENGINE.decide(_write_input(approval=_approval(resource_ref="svc:synthetic-db")))
        assert d.deny_reason is DenyReason.APPROVAL_TOOL_MISMATCH

    def test_tampered_arguments_are_denied(self) -> None:
        """威胁 T-2 在 Policy 层的拦截。Executor 还会在 Java 侧再比一次——
        两道都要，因为 Policy 到 Executor 之间仍有一个时间窗口。"""
        d = ENGINE.decide(_write_input(arguments_digest="b" * 64))
        assert d.deny_reason is DenyReason.APPROVAL_DIGEST_MISMATCH

    def test_valid_approval_allows_write(self) -> None:
        d = ENGINE.decide(_write_input())
        assert d.allowed is True
        assert d.requires_approval is True
        assert d.authorization is not None
        assert d.authorization.approval_id == "apr-1"

    @pytest.mark.parametrize("tool", catalogue.ACTION_TOOLS, ids=lambda t: t.name)
    def test_no_action_tool_can_run_without_approval(self, tool: ToolContract) -> None:
        """遍历全部动作工具，逐个证明「未审批 100% 不执行」。"""
        d = ENGINE.decide(_input(
            suggestion=ToolSuggestion(tool_name=tool.name, arguments={}),
            contract=tool,
            approval=None,
        ))
        assert d.allowed is False
        assert d.authorization is None


# --------------------------------------------------------------------------
# Policy：放行路径与纯函数性质
# --------------------------------------------------------------------------

class TestPolicyAllows:
    def test_read_tool_is_allowed(self) -> None:
        d = ENGINE.decide(_input())
        assert d.allowed is True
        assert d.requires_approval is False
        assert d.authorization is not None
        assert d.authorization.allowed is True

    def test_decision_records_evaluated_rules(self) -> None:
        d = ENGINE.decide(_input())
        assert "allowlist" in d.evaluated_rules
        assert "approval" in d.evaluated_rules

    def test_deny_reason_is_a_closed_enum(self) -> None:
        """自由文本无法聚合，M6 要统计安全红线拒绝率。"""
        d = ENGINE.decide(_input(allowed_tool_names=frozenset()))
        assert isinstance(d.deny_reason, DenyReason)


class TestPolicyPurity:
    def test_same_input_gives_same_decision(self) -> None:
        request = _input()
        first = ENGINE.decide(request)
        second = ENGINE.decide(request)
        assert (first.allowed, first.deny_reason, first.evaluated_rules) == (
            second.allowed, second.deny_reason, second.evaluated_rules
        )

    def test_engine_holds_no_state_between_calls(self) -> None:
        ENGINE.decide(_input(allowed_tool_names=frozenset()))
        assert ENGINE.decide(_input()).allowed is True

    def test_two_engines_agree(self) -> None:
        assert PolicyEngine().decide(_input()).allowed == PolicyEngine().decide(_input()).allowed
