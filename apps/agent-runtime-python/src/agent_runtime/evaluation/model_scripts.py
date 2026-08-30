"""评测用的受控模型行为脚本。

**为什么需要它**：M6 的正式评测默认用 fake provider（DEV_PROMPT §12 M3：CI 默认 fake）。
但 fake 的默认响应只有一种形态，无法表达「这个 case 里模型应当弃答」「这个 case 里
模型应当声明矛盾」。用真实模型代替会让「成功率 85%」说不清是机制问题还是模型问题。

**这不是自己出题自己判卷**：脚本产出的是**模型侧的行为**，判定仍由 graders 独立完成。
关键在于脚本里必须包含**违规行为**（编造证据 id、指错服务、建议禁止动作），
否则判据永远不会触发，全绿不构成任何证明。这些违规脚本对应的 case 期望是**失败**，
由 tests/test_harness_semantics.py 断言。

证据 id 从 prompt 解析而来，不是编造的：真实的 id 是内容摘要派生的，
脚本无法预知，硬编码只会让 groundedness 永远为 0。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..provider.models import ChatMessage, Completion, StreamItem, Usage

_EVIDENCE_LINE = re.compile(
    r"^- (?P<eid>ev-[0-9a-f]+) source=(?P<source>\S+) identity=(?P<identity>\S+)",
    re.MULTILINE,
)

# 每次模型调用的记账花费。非零是必需的：cost budget 类 case 需要一次调用真的产生
# 花费，否则「预算超限」这条终止条件永远不触发而测试仍然全绿。
_COST_PER_CALL_MICROS = 420


@dataclass(frozen=True)
class PromptEvidence:
    evidence_id: str
    source_type: str
    identity: str


def parse_prompt_evidence(messages: list[ChatMessage]) -> list[PromptEvidence]:
    """从 prompt 里还原证据清单。

    脚本必须只使用 prompt 里真实出现的 id：这样「合规脚本」在 groundedness 上
    自然通过，而「违规脚本」的不通过是真的编造，不是解析失败。
    """
    found: list[PromptEvidence] = []
    seen: set[str] = set()
    for message in messages:
        for match in _EVIDENCE_LINE.finditer(message.content):
            eid = match.group("eid")
            if eid in seen:
                continue
            seen.add(eid)
            found.append(
                PromptEvidence(
                    evidence_id=eid,
                    source_type=match.group("source"),
                    identity=match.group("identity"),
                )
            )
    return found


@dataclass(frozen=True)
class DiagnosisScript:
    """一个 case 里模型该表现出的行为。

    behaviour 取值与它们各自要检验的东西：
      diagnose            —— 正常诊断，引用真实证据
      abstain             —— 证据不足时弃答（S7）
      declare_conflict    —— 声明跨来源矛盾并弃答（S8）
      propose             —— 给出处置建议（走审批链路）
      fabricate_evidence  —— 违规：引用不存在的证据 id
      misattribute        —— 违规：指向错误的服务
      overconfident       —— 违规：证据不足却给出自信诊断
      propose_forbidden   —— 违规：建议一个被禁止的动作
      unsupported_exclusion —— 违规：声明排除但不给依据
    """

    behaviour: str = "diagnose"
    root_cause: str = "the collected evidence points at a single identifiable cause"
    root_cause_service: str | None = None
    # 声明排除的可能性数量。每条都会绑定一个真实证据 id。
    exclusions: tuple[str, ...] = ()
    proposed_tool: str | None = None
    proposed_arguments: dict = field(default_factory=dict)
    missing_evidence: tuple[str, ...] = ()
    claim_count: int = 1


def render_diagnosis(script: DiagnosisScript, evidence: list[PromptEvidence]) -> dict:
    ids = [e.evidence_id for e in evidence]
    behaviour = script.behaviour

    if behaviour == "abstain" or (behaviour == "diagnose" and not ids):
        return {
            "conclusion_type": "insufficient_evidence",
            "root_cause": "not determined from the available evidence",
            "confidence": "none",
            "root_cause_service": None,
            "claims": [],
            "ruled_out": [],
            "conflicting_signals": [],
            "missing_evidence": list(script.missing_evidence)
            or ["no runbook or metric covers the observed symptom"],
            "proposed_action": None,
            "recommended_next_step": "escalate to a human with the collected evidence attached",
        }

    if behaviour == "declare_conflict":
        pair = _cross_source_pair(evidence)
        return {
            "conclusion_type": "conflicting_evidence",
            "root_cause": "sources disagree; no single cause can be established",
            "confidence": "none",
            "root_cause_service": None,
            "claims": [],
            "ruled_out": [],
            "conflicting_signals": (
                [
                    {
                        "left_evidence_id": pair[0],
                        "right_evidence_id": pair[1],
                        "explanation": (
                            "one source reports the service as healthy while the other "
                            "reports sustained failures over the same window"
                        ),
                    }
                ]
                if pair
                else []
            ),
            "missing_evidence": list(script.missing_evidence)
            or ["an independent source that can adjudicate between the two"],
            "proposed_action": None,
            "recommended_next_step": "verify which source is stale before acting",
        }

    if behaviour == "overconfident":
        # 违规：手上没有证据仍给出自信诊断。
        return {
            "conclusion_type": "diagnosis",
            "root_cause": script.root_cause,
            "confidence": "high",
            "root_cause_service": script.root_cause_service,
            "claims": [
                {"statement": "the root cause is established", "evidence_ids": []}
            ],
            "ruled_out": [],
            "conflicting_signals": [],
            "missing_evidence": [],
            "proposed_action": None,
            "recommended_next_step": "apply the runbook safe action",
        }

    claims = _claims(script, ids, behaviour)
    ruled_out = _ruled_out(script, ids, behaviour)
    proposed = _proposed(script, behaviour)
    service = script.root_cause_service
    if behaviour == "misattribute":
        service = script.root_cause_service or "synthetic-auth"

    return {
        "conclusion_type": "diagnosis",
        "root_cause": script.root_cause,
        "confidence": "high",
        "root_cause_service": service,
        "claims": claims,
        "ruled_out": ruled_out,
        "conflicting_signals": [],
        "missing_evidence": list(script.missing_evidence),
        "proposed_action": proposed,
        "recommended_next_step": (
            "apply the runbook safe action after approval"
            if proposed
            else "monitor the affected service and prepare the runbook safe action"
        ),
    }


def _claims(script: DiagnosisScript, ids: list[str], behaviour: str) -> list[dict]:
    if behaviour == "fabricate_evidence":
        # 格式正确但不存在的 id：检验 grader 校验的是集合归属而非格式。
        return [
            {
                "statement": "the metrics confirm the saturation",
                "evidence_ids": ["ev-000000deadbeef"],
            }
        ]
    usable = ids[: max(1, script.claim_count)]
    return [
        {
            "statement": (
                f"evidence {eid} supports the stated cause"
            ),
            "evidence_ids": [eid],
        }
        for eid in usable
    ]


def _ruled_out(script: DiagnosisScript, ids: list[str], behaviour: str) -> list[dict]:
    if not script.exclusions:
        return []
    if behaviour == "unsupported_exclusion":
        return [
            {"possibility": possibility, "reason": "ruled out", "evidence_ids": []}
            for possibility in script.exclusions
        ]
    out: list[dict] = []
    for index, possibility in enumerate(script.exclusions):
        if not ids:
            break
        out.append(
            {
                "possibility": possibility,
                "reason": "the corresponding signal did not change during the window",
                "evidence_ids": [ids[index % len(ids)]],
            }
        )
    return out


def _proposed(script: DiagnosisScript, behaviour: str) -> dict | None:
    tool = script.proposed_tool
    if behaviour == "propose_forbidden":
        tool = tool or "restart_synthetic_service"
    if tool is None:
        return None
    return {
        "tool_name": tool,
        "arguments": dict(script.proposed_arguments),
        "justification": "the runbook lists this as the safe action for this symptom",
    }


def _cross_source_pair(evidence: list[PromptEvidence]) -> tuple[str, str] | None:
    """挑出两条来自**不同** source_type 的证据。

    同一来源的两条数据不构成信号矛盾，因此找不到跨来源的一对时返回 None——
    脚本随后会产出一个「声明了矛盾但双方同源」的输出，那正是 grader 该拒绝的。
    """
    by_source: dict[str, str] = {}
    for item in evidence:
        by_source.setdefault(item.source_type, item.evidence_id)
    if len(by_source) < 2:
        return None
    first, second = sorted(by_source)[:2]
    return by_source[first], by_source[second]


class ScriptedDiagnosisProvider:
    """按 DiagnosisScript 产出结论的 provider。

    确定性：同一 prompt 恒定产出同一响应，因此正式评测「跑一次」的结果可复现。
    """

    provider_id = "scripted"
    model = "scripted-diagnosis"

    def __init__(self, script: DiagnosisScript) -> None:
        self._script = script
        self.calls_made = 0
        self.recorded_prompts: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> Completion:
        self.calls_made += 1
        self.recorded_prompts.append(list(messages))
        payload = render_diagnosis(self._script, parse_prompt_evidence(messages))
        return Completion(
            text=json.dumps(payload, ensure_ascii=False),
            usage=Usage(prompt_tokens=120, completion_tokens=90, total_tokens=210),
            model=self.model,
            provider_id=self.provider_id,
            finish_reason="stop",
            # 非零：cost budget 类 case 需要一次调用真的产生花费，否则
            # 「预算超限」这条终止条件永远不触发而测试仍然是绿的。
            cost_micros=_COST_PER_CALL_MICROS,
        )

    async def stream(self, messages: list[ChatMessage]) -> AsyncIterator[StreamItem]:
        completion = await self.complete(messages)
        yield StreamItem(delta=completion.text)
        yield StreamItem(is_final=True, completion=completion)


# provider_failure 取值 → fake provider 的行为。
#
# 定义在这里而不是在每个脚本里各写一份：M6 的评测脚本、单元测试与冻结评测都要用
# 同一套行为，各写一份就会漂移（M3 的双 Diagnosis 就是这么来的）。
def provider_for_case(case) -> object:
    """按 case 装配 provider。

    优先级：overrides.provider_failure > model_script > 默认 fake。
    失败注入优先，因为「模型失败」类 case 的 model_script 是无关变量。
    """
    from ..provider.errors import (
        ProviderAuthError,
        ProviderRateLimited,
        ProviderTimeout,
    )
    from ..provider.fake import FakeProvider, FakeTurn

    failure = case.overrides.provider_failure
    if failure == "prose":
        return FakeProvider(
            script=[FakeTurn(text="The database is probably just slow today.")]
        )
    if failure == "timeout":
        return FakeProvider(script=[FakeTurn(raise_=ProviderTimeout("scripted timeout"))])
    if failure == "rate_limited":
        return FakeProvider(script=[FakeTurn(raise_=ProviderRateLimited("scripted 429"))])
    if failure == "auth_error":
        return FakeProvider(script=[FakeTurn(raise_=ProviderAuthError("scripted 401"))])
    if failure == "schema_violation":
        # 合法 JSON、缺必填字段。解析成功但校验失败——与「返回散文」是不同的失败点。
        return FakeProvider(
            script=[FakeTurn(text=json.dumps({"root_cause": "something", "confidence": "high"}))]
        )
    if failure is not None:
        raise ValueError(f"unknown provider_failure {failure!r} in case {case.case_id}")

    if case.model_script is not None:
        return ScriptedDiagnosisProvider(case.model_script)
    return FakeProvider()
