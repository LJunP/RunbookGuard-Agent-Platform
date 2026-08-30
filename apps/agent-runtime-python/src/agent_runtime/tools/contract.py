"""工具契约。

说明书 §14 要求每个工具的 Contract 必须包含全部 11 项，缺一项就不算完成。这里把
那 11 项做成 dataclass 的必填字段，使「漏了一项」变成构造期错误而不是评审时才发现。

三段分离（M0 §7）在类型上也要可见：
    ToolSuggestion  模型建议    —— 不含任何授权信息
    ToolAuthorization  Policy 裁决 —— 只有它能产生「可执行」
    ToolExecution   Executor 执行 —— 必须持有 Authorization 才能构造
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    """说明书 §14 的三级。read 允许进程内执行；propose 与 write 必须走审批。"""

    READ = "read"
    PROPOSE = "propose"
    WRITE = "write"


class CancelSemantics(str, Enum):
    """ADR-0001 C-6 的裁决：进程内 async 无法硬取消阻塞调用，因此分级定义。

    声明成协作式而不假装能硬取消，是为了让契约不写下无法实现的承诺。
    """

    COOPERATIVE = "cooperative"
    FORCEFUL = "forceful"


class ToolEnvironment(str, Enum):
    """allowed environment。第一版只有 synthetic-lab，真实环境需要新 ADR 才能加入。"""

    SYNTHETIC_LAB = "synthetic-lab"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_seconds: tuple[float, ...]
    retriable_failures: frozenset[str]

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


@dataclass(frozen=True)
class ResourceBinding:
    """principal + tenant + resource binding（说明书 §14 第 3 项）。

    tenant 与 principal 由调用方从 Control Plane 的权威身份派生，**绝不**从模型建议
    或工具参数中读取——客户端自报的 tenant 是不可信输入（威胁 T-4）。
    """

    principal_id: str
    tenant_id: str
    resource_ref: str

    def __post_init__(self) -> None:
        for name in ("principal_id", "tenant_id", "resource_ref"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class ToolContract:
    """11 项全部必填，没有默认值可省略的项。"""

    # 1
    name: str
    version: str
    description: str
    # 2
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # 3 —— binding 的**要求**在契约里，具体值在调用时给
    requires_principal: bool
    requires_tenant: bool
    resource_kind: str
    # 4
    risk: RiskLevel
    # 5
    timeout_seconds: float
    cancel_semantics: CancelSemantics
    max_result_bytes: int
    # 6
    retry: RetryPolicy
    # 7 —— 幂等键的构造方式。read 类工具无副作用，故为 None
    idempotency_key_template: str | None
    # 8
    audit_fields: tuple[str, ...]
    # 9
    allowed_environments: frozenset[ToolEnvironment]
    # 10
    typed_failures: frozenset[str]
    # 11 —— 是否需要人工审批。write 必须为 True
    requires_approval: bool

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ValueError("name and version are required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_result_bytes <= 0:
            raise ValueError("max_result_bytes must be positive")
        if not self.typed_failures:
            raise ValueError(f"{self.name}: typed_failures must not be empty")
        if not self.allowed_environments:
            raise ValueError(f"{self.name}: allowed_environments must not be empty")
        if not self.audit_fields:
            raise ValueError(f"{self.name}: audit_fields must not be empty")

        # 写动作没有审批就是整个安全模型失效。这条在构造期挡住，不留给评审。
        if self.risk is RiskLevel.WRITE and not self.requires_approval:
            raise ValueError(f"{self.name}: write-risk tools must require approval")
        # 写动作必须幂等：M2 的「至少一次投递 + 幂等执行」推论——
        # 自身不幂等的动作工具不允许进入工具集（ADR-0003）。
        if self.risk is RiskLevel.WRITE and not self.idempotency_key_template:
            raise ValueError(
                f"{self.name}: write-risk tools must declare an idempotency key template"
            )
        # 动作型工具必须能被强制取消（M0 §9 Sandbox 分级）。
        if self.risk is RiskLevel.WRITE and self.cancel_semantics is not CancelSemantics.FORCEFUL:
            raise ValueError(f"{self.name}: write-risk tools must be forcefully cancellable")
        if self.risk is RiskLevel.READ and self.requires_approval:
            raise ValueError(
                f"{self.name}: read-only tools must not require approval; "
                "requiring it would make evidence gathering need human sign-off"
            )

    def is_write(self) -> bool:
        return self.risk is RiskLevel.WRITE


@dataclass(frozen=True)
class ToolSuggestion:
    """模型的建议。**不含任何授权信息**——这是三段分离的第一段。

    刻意不给它任何方法能产生 Authorization：模型输出是建议，不是权限（M0 §9）。
    """

    tool_name: str
    arguments: dict[str, Any]
    rationale: str = ""
    # 建议可能来自被注入污染的上下文，因此记录它是在哪一步产生的，便于审计溯源。
    suggested_at_step: int = 0


@dataclass(frozen=True)
class ToolAuthorization:
    """Policy 的裁决结果。只有 PolicyEngine 能构造出 allowed=True 的实例。

    `_issuer` 是构造守卫：任何绕过 PolicyEngine 直接 new 一个 allowed=True 的尝试
    都会失败。这让「谁有权放行」在类型层面就是唯一的。
    """

    tool_name: str
    binding: ResourceBinding
    arguments: dict[str, Any]
    arguments_digest: str
    allowed: bool
    reason: str
    requires_approval: bool
    approval_id: str | None = None
    _issuer: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.allowed and self._issuer != _POLICY_ISSUER_TOKEN:
            raise PermissionError(
                "ToolAuthorization(allowed=True) may only be issued by PolicyEngine"
            )


# 不是密钥，只是一个进程内的构造守卫，防止误用而非防御攻击者。
_POLICY_ISSUER_TOKEN = "policy-engine"


def issue_authorization(
    *,
    tool_name: str,
    binding: ResourceBinding,
    arguments: dict[str, Any],
    arguments_digest: str,
    allowed: bool,
    reason: str,
    requires_approval: bool,
    approval_id: str | None = None,
) -> ToolAuthorization:
    """PolicyEngine 专用。放在模块级而非类方法，使 grep `issue_authorization`
    就能穷举所有放行点。"""
    return ToolAuthorization(
        tool_name=tool_name,
        binding=binding,
        arguments=arguments,
        arguments_digest=arguments_digest,
        allowed=allowed,
        reason=reason,
        requires_approval=requires_approval,
        approval_id=approval_id,
        _issuer=_POLICY_ISSUER_TOKEN if allowed else "",
    )
