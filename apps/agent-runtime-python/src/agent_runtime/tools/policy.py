"""Policy 引擎 —— 三段分离的中间段（M0 §7）。

说明书要求 Policy「必须是可独立单测的纯函数式组件」。这里的落实方式：
`decide()` 不做 IO、不读全局状态、不看时钟。所有外部事实由 `PolicyInput` 显式传入。

**默认拒绝**：没有任何一条规则显式放行时，结果是拒绝。新增工具若忘记加入 allowlist，
表现为不可用而不是可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .contract import (
    ResourceBinding,
    RiskLevel,
    ToolAuthorization,
    ToolContract,
    ToolEnvironment,
    ToolSuggestion,
    issue_authorization,
)


class DenyReason(str, Enum):
    """拒绝原因是封闭枚举，不是自由文本：M6 要统计安全红线拒绝率，
    自由文本无法聚合。"""

    UNKNOWN_TOOL = "unknown_tool"
    NOT_IN_ALLOWLIST = "not_in_allowlist"
    MISSING_BINDING = "missing_binding"
    TENANT_MISMATCH = "tenant_mismatch"
    RESOURCE_NOT_PERMITTED = "resource_not_permitted"
    ENVIRONMENT_NOT_ALLOWED = "environment_not_allowed"
    SCHEMA_VIOLATION = "schema_violation"
    NON_CANONICALIZABLE_ARGUMENTS = "non_canonicalizable_arguments"
    APPROVAL_REQUIRED_BUT_ABSENT = "approval_required_but_absent"
    # PENDING 与 REJECTED/EXPIRED 是不同的处境：前者应挂起等人，后者应放弃。
    # 合成一个 reason 会让调用方无法区分「等一下」和「别想了」。
    APPROVAL_PENDING = "approval_pending"
    APPROVAL_NOT_GRANTED = "approval_not_granted"
    APPROVAL_TOOL_MISMATCH = "approval_tool_mismatch"
    APPROVAL_DIGEST_MISMATCH = "approval_digest_mismatch"
    TOOL_CALL_BUDGET_EXHAUSTED = "tool_call_budget_exhausted"
    WRITE_ACTION_IN_READ_ONLY_RUN = "write_action_in_read_only_run"


@dataclass(frozen=True)
class ApprovalFact:
    """来自 Java Control Plane 的审批事实。

    这是**事实的快照**，不是授权凭据：Policy 用它判断「审批是否已批准且参数一致」，
    但真正的放行仍要求 Executor 在执行前调 Control Plane 的 consume 端点做四项比对
    （M0 INV-4）。Policy 通过不等于可以执行。
    """

    approval_id: str
    tool_name: str
    resource_ref: str
    arguments_digest: str
    decision: str
    consumed: bool


@dataclass(frozen=True)
class PolicyInput:
    """决策所需的全部外部事实。没有隐式依赖，因此 decide() 是纯函数。"""

    suggestion: ToolSuggestion
    contract: ToolContract | None
    binding: ResourceBinding
    environment: ToolEnvironment
    allowed_tool_names: frozenset[str]
    permitted_resources: frozenset[str]
    arguments_digest: str | None
    schema_errors: tuple[str, ...] = ()
    approval: ApprovalFact | None = None
    tool_calls_used: int = 0
    tool_call_budget: int = 0
    read_only_run: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    deny_reason: DenyReason | None
    requires_approval: bool
    authorization: ToolAuthorization | None = None
    # 命中的规则序列，用于审计与调试。顺序即求值顺序。
    evaluated_rules: tuple[str, ...] = field(default_factory=tuple)


class PolicyEngine:
    """无状态。所有事实通过 PolicyInput 传入，因此可以自由并发调用。"""

    def decide(self, request: PolicyInput) -> PolicyDecision:
        rules: list[str] = []

        def deny(reason: DenyReason, detail: str) -> PolicyDecision:
            return PolicyDecision(
                allowed=False,
                reason=detail,
                deny_reason=reason,
                requires_approval=False,
                authorization=None,
                evaluated_rules=tuple(rules),
            )

        # 1. 工具必须存在于目录。模型可以建议任意字符串。
        rules.append("known_tool")
        contract = request.contract
        if contract is None:
            return deny(
                DenyReason.UNKNOWN_TOOL,
                f"tool {request.suggestion.tool_name!r} is not in the catalogue",
            )

        # 2. allowlist。默认拒绝的落点：不在名单里就是不允许，无需理由。
        rules.append("allowlist")
        if contract.name not in request.allowed_tool_names:
            return deny(
                DenyReason.NOT_IN_ALLOWLIST,
                f"tool {contract.name} is not in this run's allowlist",
            )

        # 3. binding 完整性（威胁 T-4）。
        rules.append("binding_present")
        if contract.requires_principal and not request.binding.principal_id:
            return deny(DenyReason.MISSING_BINDING, "principal_id is required but absent")
        if contract.requires_tenant and not request.binding.tenant_id:
            return deny(DenyReason.MISSING_BINDING, "tenant_id is required but absent")

        # 4. resource 必须在本 run 的许可集合内。
        #    集合由 Control Plane 派生，不接受模型或参数自报。
        rules.append("resource_permitted")
        if request.binding.resource_ref not in request.permitted_resources:
            return deny(
                DenyReason.RESOURCE_NOT_PERMITTED,
                f"resource {request.binding.resource_ref} is not permitted for this run",
            )

        # 5. 环境。第一版只有 synthetic-lab；真实环境需新 ADR。
        rules.append("environment")
        if request.environment not in contract.allowed_environments:
            return deny(
                DenyReason.ENVIRONMENT_NOT_ALLOWED,
                f"tool {contract.name} may not run in {request.environment.value}",
            )

        # 6. 参数 schema。校验在 Policy 之前完成，这里只消费结果——
        #    让 Policy 保持无 IO 且不依赖 jsonschema 实现。
        rules.append("schema")
        if request.schema_errors:
            return deny(
                DenyReason.SCHEMA_VIOLATION,
                "arguments violate input schema: " + "; ".join(request.schema_errors),
            )

        # 7. 参数必须可规范化，否则无法产生摘要，审批也就无从绑定。
        rules.append("canonicalizable")
        if not request.arguments_digest:
            return deny(
                DenyReason.NON_CANONICALIZABLE_ARGUMENTS,
                "arguments could not be canonicalized; no digest available",
            )

        # 8. tool-call 预算（M0 终止条件 4）。
        rules.append("tool_call_budget")
        if request.tool_call_budget and request.tool_calls_used >= request.tool_call_budget:
            return deny(
                DenyReason.TOOL_CALL_BUDGET_EXHAUSTED,
                f"tool call budget exhausted ({request.tool_calls_used}/{request.tool_call_budget})",
            )

        # 9. 只读 run 里不允许任何写动作。评测中的「禁止动作」类 case 依赖这一条。
        rules.append("read_only_run")
        if request.read_only_run and contract.risk is not RiskLevel.READ:
            return deny(
                DenyReason.WRITE_ACTION_IN_READ_ONLY_RUN,
                f"run is read-only; {contract.name} has risk={contract.risk.value}",
            )

        # 10. 审批。写动作没有已批准且参数一致的审批就绝不放行（M4 Gate 硬条件）。
        rules.append("approval")
        if contract.requires_approval:
            approval = request.approval
            if approval is None:
                return deny(
                    DenyReason.APPROVAL_REQUIRED_BUT_ABSENT,
                    f"{contract.name} requires approval but none was supplied",
                )
            if approval.decision == "PENDING" and not approval.consumed:
                # 尚未决策：调用方应挂起等人，而不是当作失败放弃。
                return deny(
                    DenyReason.APPROVAL_PENDING,
                    f"approval {approval.approval_id} is still pending a human decision",
                )
            if approval.decision != "APPROVED" or approval.consumed:
                return deny(
                    DenyReason.APPROVAL_NOT_GRANTED,
                    f"approval {approval.approval_id} is not usable "
                    f"(decision={approval.decision}, consumed={approval.consumed})",
                )
            if approval.tool_name != contract.name or approval.resource_ref != request.binding.resource_ref:
                return deny(
                    DenyReason.APPROVAL_TOOL_MISMATCH,
                    f"approval {approval.approval_id} was issued for "
                    f"{approval.tool_name}/{approval.resource_ref}, not "
                    f"{contract.name}/{request.binding.resource_ref}",
                )
            # 威胁 T-2 在 Policy 层的第一道拦截。Executor 还会在 Java 侧再比一次——
            # 两道都要，因为 Policy 到 Executor 之间仍有一个时间窗口。
            if approval.arguments_digest != request.arguments_digest:
                return deny(
                    DenyReason.APPROVAL_DIGEST_MISMATCH,
                    f"arguments digest mismatch: approved={approval.arguments_digest} "
                    f"actual={request.arguments_digest}",
                )

        return PolicyDecision(
            allowed=True,
            reason="all policy rules satisfied",
            deny_reason=None,
            requires_approval=contract.requires_approval,
            authorization=issue_authorization(
                tool_name=contract.name,
                binding=request.binding,
                arguments=dict(request.suggestion.arguments),
                arguments_digest=request.arguments_digest,
                allowed=True,
                reason="all policy rules satisfied",
                requires_approval=contract.requires_approval,
                approval_id=request.approval.approval_id if request.approval else None,
            ),
            evaluated_rules=tuple(rules),
        )
