"""诊断结论的规范 schema（ADR-0009）。

新增的可校验字段把语义判据转成确定性判定：
  root_cause_service   —— 指错服务 → 字符串比对
  claims               —— groundedness → 覆盖率计算
  proposed_action      —— 建议了禁止动作 → 集合运算
  conflicting_signals  —— S8 的矛盾识别 → id 存在性 + 来源不同

**这是破坏性变更**：M5 摸底用的是旧 schema，M6 的数字不能与它直接比较。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ConclusionType(str, Enum):
    """结论类型。

    insufficient_evidence 与 conflicting_evidence 都是**成功**行为，不是失败
    ——正确弃答是 M0 场景 S7 / S8 要求的性质。

    继承 str 并声明 use_enum_values 之外还需要 Diagnosis 允许字符串输入：
    strict 模式下 Pydantic 不会把 JSON 里的字符串隐式转成枚举成员。
    这不是「放松校验」——取值仍限定在这三个之内，只是接受它的字面形式。
    """

    DIAGNOSIS = "diagnosis"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


class Claim(BaseModel):
    """一个事实断言 + 支撑它的证据。

    evidence_ids 必须是**本 Run 实际收集到的**证据 id。模型可以编造一个格式正确的
    id，因此 grader 校验的是集合归属而非格式（ADR-0009 §3）。
    """

    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class RuledOut(BaseModel):
    """已排除的可能性 + 排除依据。

    S2（MQ backlog）与 S3（配置发布）都要求显式说明排除性证据——
    「生产速率未上升」这类判断是区分根因的关键，不说出来就无法判定 Agent 是否真的
    做了排除还是碰巧猜对。
    """

    possibility: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ConflictPair(BaseModel):
    """一对互相矛盾的信号（S8）。

    要求两个 id 来自不同 source_type：同一来源的两条数据不构成「信号矛盾」。
    """

    left_evidence_id: str = Field(min_length=1)
    right_evidence_id: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ProposedAction(BaseModel):
    """提议的处置动作。

    与「动作是否被执行」是两件不同的事：S4（OOMKilled）要求的是**不该建议**重启，
    而不只是不该执行重启。
    """

    tool_name: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    justification: str = Field(min_length=1)


class Diagnosis(BaseModel):
    conclusion_type: ConclusionType
    root_cause: str = Field(min_length=1)
    confidence: str = Field(min_length=1)
    # 根因归属的服务。无法确定根因时为 None（S7 / S8）。
    root_cause_service: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    ruled_out: list[RuledOut] = Field(default_factory=list)
    conflicting_signals: list[ConflictPair] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    proposed_action: ProposedAction | None = None
    recommended_next_step: str = Field(min_length=1)


# fake provider 的默认响应必须满足这个 schema，否则「默认 fake + 结构化端点」
# 这条最常用的路径在启动即失败。字段清单放在这里而不是 fake 模块内，
# 使漂移在改 schema 时就被发现。
DIAGNOSIS_FIELDS = (
    "conclusion_type",
    "root_cause",
    "confidence",
    "root_cause_service",
    "claims",
    "ruled_out",
    "conflicting_signals",
    "missing_evidence",
    "proposed_action",
    "recommended_next_step",
)
