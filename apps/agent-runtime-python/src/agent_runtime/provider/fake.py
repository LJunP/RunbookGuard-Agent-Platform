"""fake provider（ADR-0005 §7）。

CI 默认用它：确定性、零成本、零网络。

它必须能模拟**每一种**失败，否则基于它的测试就成了「自己出题自己判卷」——
fake 只会返回完美响应时，Adapter 必然能处理，那样的全绿不构成任何证明。
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, field
from typing import AsyncIterator

from .errors import ProviderBadResponse, ProviderBudgetExceeded, ProviderError
from .models import ChatMessage, Completion, StreamItem, Usage


@dataclass
class FakeTurn:
    """脚本中的一轮行为。text 与 raise_ 互斥。"""

    text: str | None = None
    raise_: ProviderError | None = None
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=lambda: Usage(
        prompt_tokens=10, completion_tokens=5, total_tokens=15
    ))
    # 流式中途断开：不产出终止标记，用于验证「部分输出不算结果」。
    break_stream: bool = False

    def __post_init__(self) -> None:
        if self.text is not None and self.raise_ is not None:
            raise ValueError("FakeTurn takes either text or raise_, not both")


class FakeProvider:
    def __init__(
        self,
        script: list[FakeTurn] | None = None,
        *,
        strict_script: bool = False,
        max_calls: int = 1000,
        provider_id: str = "fake",
        model: str = "fake-model",
    ) -> None:
        self._script = list(script or [])
        self._strict_script = strict_script
        self._cursor = 0
        self.max_calls = max_calls
        self.provider_id = provider_id
        self.model = model
        self.calls_made = 0
        self.recorded_prompts: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> Completion:
        turn = self._next_turn(messages)
        if turn.raise_ is not None:
            raise turn.raise_
        text = turn.text if turn.text is not None else self._deterministic_text(messages)
        return Completion(
            text=text,
            usage=turn.usage,
            model=self.model,
            provider_id=self.provider_id,
            finish_reason=turn.finish_reason,
            attempts=1,
            cost_micros=0,
        )

    async def stream(self, messages: list[ChatMessage]) -> AsyncIterator[StreamItem]:
        turn = self._next_turn(messages)
        if turn.raise_ is not None:
            raise turn.raise_
        text = turn.text if turn.text is not None else self._deterministic_text(messages)

        # 按词切分，模拟真实流式的分块粒度。
        parts = text.split(" ")
        for index, part in enumerate(parts):
            piece = part if index == len(parts) - 1 else part + " "
            yield StreamItem(delta=piece)

        if turn.break_stream:
            raise ProviderBadResponse(
                "stream ended without terminator; partial output is not a result"
            )

        yield StreamItem(
            is_final=True,
            completion=Completion(
                text=text,
                usage=turn.usage,
                model=self.model,
                provider_id=self.provider_id,
                finish_reason=turn.finish_reason,
                attempts=1,
                cost_micros=0,
            ),
        )

    def _next_turn(self, messages: list[ChatMessage]) -> FakeTurn:
        if self.calls_made >= self.max_calls:
            raise ProviderBudgetExceeded(
                f"fake provider call limit reached ({self.max_calls})"
            )
        self.calls_made += 1
        self.recorded_prompts.append(list(messages))

        if self._cursor < len(self._script):
            turn = self._script[self._cursor]
            self._cursor += 1
            return turn
        if self._strict_script:
            # 脚本耗尽时显式失败，而不是回退到默认响应——静默回退会让
            # 「我以为脚本还有一轮」这类测试错误变成通过。
            raise AssertionError(
                f"fake provider script exhausted after {len(self._script)} turn(s)"
            )
        return FakeTurn()

    def _deterministic_text(self, messages: list[ChatMessage]) -> str:
        """同一 prompt 恒定产出同一响应。CI 的可复现性依赖这一点。

        默认响应必须满足 agent_runtime.schemas.Diagnosis（含 ADR-0009 新增的
        可校验字段）：否则「默认 fake + /v1/diagnose」这条最常用的路径会启动即失败。

        evidence_ids 从 prompt 里解析出来而不是编造：grader 校验 claim 引用的 id
        必须存在于本 Run 的证据集合中，编造的 id 会让 fake 的默认响应在
        groundedness 上直接不合格，而那不是 fake 该表达的行为。
        """
        key = json.dumps([m.model_dump() for m in messages], sort_keys=True, ensure_ascii=False)
        digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
        token = struct.unpack("<Q", digest)[0]
        evidence_ids = _evidence_ids_in(messages)
        return json.dumps(
            {
                "conclusion_type": "diagnosis" if evidence_ids else "insufficient_evidence",
                "root_cause": f"fake-diagnosis-{token % 100000}",
                "confidence": ("high", "medium", "low")[token % 3],
                "root_cause_service": _service_in(messages),
                "claims": [
                    {
                        "statement": "the evidence supports a single identifiable cause",
                        "evidence_ids": evidence_ids[:2],
                    }
                ]
                if evidence_ids
                else [],
                "ruled_out": [],
                "conflicting_signals": [],
                "missing_evidence": [] if evidence_ids else ["no usable evidence was collected"],
                "proposed_action": None,
                "recommended_next_step": "no action; this is a fake provider response",
            },
            ensure_ascii=False,
        )


_EVIDENCE_PATTERN = re.compile(r"\b(ev-[0-9a-f]{6,})\b")
_SERVICE_PATTERN = re.compile(r"\b(synthetic-[a-z]+)\b")


def _evidence_ids_in(messages: list[ChatMessage]) -> list[str]:
    """从 prompt 里抽出证据 id。

    fake 不编造 id：编造的 id 不在本 Run 的证据集合里，会让默认响应在
    groundedness 判据上失败——而 fake 的默认行为应当是「一个合规的产出」。
    """
    found: list[str] = []
    for message in messages:
        for match in _EVIDENCE_PATTERN.finditer(message.content):
            candidate = match.group(1)
            if candidate not in found:
                found.append(candidate)
    return found


def _service_in(messages: list[ChatMessage]) -> str | None:
    for message in messages:
        if message.role != "user":
            continue
        match = _SERVICE_PATTERN.search(message.content)
        if match:
            return match.group(1)
    return None
