"""Provider 层的失败测试。

按铁律二先于实现存在。M3 的 Gate 条件只有一句：模型失败不能产生假成功。
因此这里每一条都是失败路径，happy path 只作为对照。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from agent_runtime.provider.errors import (
    MalformedJsonError,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderBudgetExceeded,
    ProviderContentFiltered,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    SchemaViolationError,
)
from agent_runtime.provider.fake import FakeProvider, FakeTurn
from agent_runtime.provider.models import ChatMessage, ProviderConfig
from agent_runtime.provider.openai_compatible import OpenAICompatibleProvider
from agent_runtime.provider.structured import parse_structured
from agent_runtime.redaction import redact
from agent_runtime.schemas import DIAGNOSIS_FIELDS, Diagnosis
from pydantic import BaseModel, Field


BASE_URL = "https://provider.test/v1"


class StrictDiagnosis(BaseModel):
    """比规范 schema 更窄的被试，用于 schema 违规测试。

    刻意与 agent_runtime.schemas.Diagnosis 分开：用同一个 schema 既测「合法输入通过」
    又测「非法输入拒绝」时，容易在改 schema 时让两类测试同时失效。
    """

    root_cause: str = Field(min_length=1)
    confidence: str
    evidence_ids: list[str]


def _config(**overrides) -> ProviderConfig:
    defaults = dict(
        base_url=BASE_URL,
        model="test-model",
        api_key="sk-test-key-value-do-not-log",
        timeout_seconds=1.0,
        max_attempts=3,
        backoff_seconds=(0.0, 0.0),
        max_calls=50,
    )
    defaults.update(overrides)
    return ProviderConfig(**defaults)


def _envelope(content: str, *, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": finish_reason}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


PROMPT = [ChatMessage(role="user", content="diagnose the incident")]


# --------------------------------------------------------------------------
# 1. 结构化输出：不合法 JSON
# --------------------------------------------------------------------------

class TestMalformedJson:
    def test_plain_prose_is_rejected(self) -> None:
        with pytest.raises(MalformedJsonError):
            parse_structured("The database seems slow, maybe restart it.", StrictDiagnosis)

    def test_truncated_json_is_not_repaired(self) -> None:
        with pytest.raises(MalformedJsonError):
            parse_structured('{"root_cause": "pool exhausted", "confid', StrictDiagnosis)

    def test_single_quotes_are_not_converted(self) -> None:
        with pytest.raises(MalformedJsonError):
            parse_structured("{'root_cause': 'x', 'confidence': 'high', 'evidence_ids': []}", StrictDiagnosis)

    def test_trailing_comma_is_not_removed(self) -> None:
        with pytest.raises(MalformedJsonError):
            parse_structured('{"root_cause": "x", "confidence": "high", "evidence_ids": [],}', StrictDiagnosis)

    def test_json_embedded_in_prose_is_rejected(self) -> None:
        """不从散文里「找出看起来像 JSON 的部分」。"""
        text = 'Here is my answer: {"root_cause": "x", "confidence": "high", "evidence_ids": []} Hope it helps!'
        with pytest.raises(MalformedJsonError):
            parse_structured(text, StrictDiagnosis)

    def test_empty_text_is_rejected(self) -> None:
        with pytest.raises(MalformedJsonError):
            parse_structured("", StrictDiagnosis)

    def test_json_array_at_top_level_is_rejected(self) -> None:
        with pytest.raises(MalformedJsonError):
            parse_structured('[{"root_cause": "x"}]', StrictDiagnosis)


# --------------------------------------------------------------------------
# 2. 结构化输出：schema 违规
# --------------------------------------------------------------------------

class TestSchemaViolation:
    def test_missing_required_field(self) -> None:
        with pytest.raises(SchemaViolationError):
            parse_structured('{"root_cause": "x", "confidence": "high"}', StrictDiagnosis)

    def test_wrong_type_is_not_coerced(self) -> None:
        with pytest.raises(SchemaViolationError):
            parse_structured(
                '{"root_cause": "x", "confidence": "high", "evidence_ids": "ev-1"}', StrictDiagnosis
            )

    def test_empty_required_string_is_rejected(self) -> None:
        with pytest.raises(SchemaViolationError):
            parse_structured(
                '{"root_cause": "", "confidence": "high", "evidence_ids": []}', StrictDiagnosis
            )

    def test_missing_field_is_not_defaulted(self) -> None:
        """schema 不匹配时不填默认值——填了就成了「假成功」。"""
        with pytest.raises(SchemaViolationError) as exc:
            parse_structured('{"confidence": "high", "evidence_ids": []}', StrictDiagnosis)
        assert "root_cause" in str(exc.value)


# --------------------------------------------------------------------------
# 3. 结构化输出：允许但必须标记的解包
# --------------------------------------------------------------------------

class TestFencedJsonUnwrapping:
    def test_markdown_fence_is_stripped_and_flagged(self) -> None:
        text = '```json\n{"root_cause": "pool exhausted", "confidence": "high", "evidence_ids": ["ev-1"]}\n```'
        result = parse_structured(text, StrictDiagnosis)
        assert result.value.root_cause == "pool exhausted"
        assert result.unwrapped is True, "解包必须被标记，否则就是静默修补"

    def test_bare_fence_without_language_is_stripped(self) -> None:
        text = '```\n{"root_cause": "x", "confidence": "low", "evidence_ids": []}\n```'
        assert parse_structured(text, StrictDiagnosis).unwrapped is True

    def test_clean_json_is_not_flagged_as_unwrapped(self) -> None:
        text = '{"root_cause": "x", "confidence": "low", "evidence_ids": []}'
        assert parse_structured(text, StrictDiagnosis).unwrapped is False

    def test_fence_containing_non_json_still_fails(self) -> None:
        with pytest.raises(MalformedJsonError):
            parse_structured("```json\nnot json\n```", StrictDiagnosis)


# --------------------------------------------------------------------------
# 4. Provider：超时
# --------------------------------------------------------------------------

class TestTimeout:
    @respx.mock
    async def test_timeout_raises_typed_failure(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderTimeout) as exc:
            await provider.complete(PROMPT)
        assert exc.value.failure_class == "provider_failure"
        assert exc.value.retriable is True

    @respx.mock
    async def test_timeout_retries_are_bounded(self) -> None:
        route = respx.post(f"{BASE_URL}/chat/completions").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        provider = OpenAICompatibleProvider(_config(max_attempts=3))
        with pytest.raises(ProviderTimeout) as exc:
            await provider.complete(PROMPT)
        assert route.call_count == 3, "重试必须有界"
        assert exc.value.attempt == 3

    @respx.mock
    async def test_recovers_when_retry_succeeds(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            side_effect=[
                httpx.ReadTimeout("timed out"),
                httpx.Response(200, json=_envelope('{"ok": true}')),
            ]
        )
        provider = OpenAICompatibleProvider(_config())
        completion = await provider.complete(PROMPT)
        assert completion.text == '{"ok": true}'
        assert completion.attempts == 2


# --------------------------------------------------------------------------
# 5. Provider：限流
# --------------------------------------------------------------------------

class TestRateLimit:
    @respx.mock
    async def test_429_is_retried_then_raised(self) -> None:
        route = respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        )
        provider = OpenAICompatibleProvider(_config(max_attempts=2))
        with pytest.raises(ProviderRateLimited) as exc:
            await provider.complete(PROMPT)
        assert route.call_count == 2
        assert exc.value.retriable is True

    @respx.mock
    async def test_429_then_success(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow"}),
                httpx.Response(200, json=_envelope('{"ok": 1}')),
            ]
        )
        provider = OpenAICompatibleProvider(_config())
        assert (await provider.complete(PROMPT)).text == '{"ok": 1}'

    @respx.mock
    async def test_retry_after_header_is_honoured(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(429, headers={"retry-after": "0"}, json={})
        )
        provider = OpenAICompatibleProvider(_config(max_attempts=1))
        with pytest.raises(ProviderRateLimited) as exc:
            await provider.complete(PROMPT)
        assert exc.value.retry_after == 0.0


# --------------------------------------------------------------------------
# 6. Provider：空响应 / 截断 / 包络异常
# --------------------------------------------------------------------------

class TestBadResponse:
    @respx.mock
    async def test_empty_choices_is_rejected(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [], "usage": {}})
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderBadResponse):
            await provider.complete(PROMPT)

    @respx.mock
    async def test_empty_content_is_rejected(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json=_envelope(""))
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderBadResponse):
            await provider.complete(PROMPT)

    @respx.mock
    async def test_non_json_body_is_rejected(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, text="<html>gateway error</html>")
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderBadResponse):
            await provider.complete(PROMPT)

    @respx.mock
    async def test_length_finish_reason_is_rejected(self) -> None:
        """截断的响应不算成功——它看起来是 200，但内容不完整。"""
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_envelope('{"root_cause": "poo', finish_reason="length")
            )
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderBadResponse) as exc:
            await provider.complete(PROMPT)
        assert "truncat" in str(exc.value).lower() or "length" in str(exc.value).lower()

    @respx.mock
    async def test_content_filter_is_typed_separately(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200, json=_envelope("", finish_reason="content_filter")
            )
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderContentFiltered) as exc:
            await provider.complete(PROMPT)
        assert exc.value.failure_class == "safety_rule_triggered"

    @respx.mock
    async def test_missing_usage_defaults_to_zero_not_crash(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
            )
        )
        provider = OpenAICompatibleProvider(_config())
        completion = await provider.complete(PROMPT)
        assert completion.usage.total_tokens == 0


# --------------------------------------------------------------------------
# 7. Provider：鉴权与不可重试错误
# --------------------------------------------------------------------------

class TestAuthAndNonRetriable:
    @respx.mock
    async def test_401_is_not_retried(self) -> None:
        route = respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": "invalid api key"})
        )
        provider = OpenAICompatibleProvider(_config(max_attempts=3))
        with pytest.raises(ProviderAuthError) as exc:
            await provider.complete(PROMPT)
        assert route.call_count == 1, "鉴权失败重试只会浪费预算并可能触发风控"
        assert exc.value.retriable is False
        assert exc.value.failure_class == "authorization_failed"

    @respx.mock
    async def test_403_is_auth_error(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        with pytest.raises(ProviderAuthError):
            await OpenAICompatibleProvider(_config()).complete(PROMPT)

    @respx.mock
    async def test_400_is_not_retried(self) -> None:
        route = respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(400, json={"error": "unsupported response_format"})
        )
        with pytest.raises(ProviderBadResponse):
            await OpenAICompatibleProvider(_config(max_attempts=3)).complete(PROMPT)
        assert route.call_count == 1

    @respx.mock
    async def test_503_is_retried(self) -> None:
        route = respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(503, json={"error": "unavailable"})
        )
        with pytest.raises(ProviderUnavailable):
            await OpenAICompatibleProvider(_config(max_attempts=2)).complete(PROMPT)
        assert route.call_count == 2


# --------------------------------------------------------------------------
# 8. Provider：预算闸
# --------------------------------------------------------------------------

class TestBudgetGuard:
    @respx.mock
    async def test_call_limit_is_enforced(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json=_envelope("{}"))
        )
        provider = OpenAICompatibleProvider(_config(max_calls=2))
        await provider.complete(PROMPT)
        await provider.complete(PROMPT)
        with pytest.raises(ProviderBudgetExceeded) as exc:
            await provider.complete(PROMPT)
        assert exc.value.failure_class == "cost_budget_exhausted"

    @respx.mock
    async def test_retries_count_toward_call_limit(self) -> None:
        """重试也是真实调用，必须计入上限——否则一个循环 bug 能烧掉预算。"""
        respx.post(f"{BASE_URL}/chat/completions").mock(
            side_effect=httpx.ReadTimeout("t")
        )
        provider = OpenAICompatibleProvider(_config(max_calls=2, max_attempts=5))
        with pytest.raises((ProviderBudgetExceeded, ProviderTimeout)):
            await provider.complete(PROMPT)
        assert provider.calls_made <= 2

    @respx.mock
    async def test_cost_limit_is_enforced(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json=_envelope("{}"))
        )
        provider = OpenAICompatibleProvider(
            _config(max_cost_micros=1, price_per_1k_prompt_micros=1000,
                    price_per_1k_completion_micros=1000)
        )
        await provider.complete(PROMPT)
        with pytest.raises(ProviderBudgetExceeded):
            await provider.complete(PROMPT)


# --------------------------------------------------------------------------
# 9. Secret 不泄漏（威胁 T-3）
# --------------------------------------------------------------------------

class TestSecretRedaction:
    def test_api_key_is_masked_in_config_repr(self) -> None:
        cfg = _config()
        assert "sk-test-key-value-do-not-log" not in repr(cfg)
        assert "sk-test-key-value-do-not-log" not in str(cfg)

    def test_key_patterns_are_redacted(self) -> None:
        assert "sk-abcdefghijklmnopqrstuv" not in redact("key=sk-abcdefghijklmnopqrstuv")
        assert "hunter2secret" not in redact("password=hunter2secret")
        assert "Bearer abcdef1234567890" not in redact("Authorization: Bearer abcdef1234567890")

    def test_clean_text_survives(self) -> None:
        clean = "connection pool exhausted for synthetic-orders"
        assert redact(clean) == clean

    @respx.mock
    async def test_error_message_does_not_leak_key(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": "invalid key sk-test-key-value-do-not-log"})
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderAuthError) as exc:
            await provider.complete(PROMPT)
        assert "sk-test-key-value-do-not-log" not in str(exc.value)


# --------------------------------------------------------------------------
# 10. SSE 流式
# --------------------------------------------------------------------------

def _sse(*chunks: str, done: bool = True) -> str:
    lines = []
    for c in chunks:
        payload = {"choices": [{"delta": {"content": c}, "finish_reason": None}]}
        lines.append(f"data: {json.dumps(payload)}\n\n")
    if done:
        lines.append('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        lines.append("data: [DONE]\n\n")
    return "".join(lines)


class TestStreaming:
    @respx.mock
    async def test_complete_stream_yields_chunks_then_completion(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200, text=_sse("hello ", "world"),
                headers={"content-type": "text/event-stream"},
            )
        )
        provider = OpenAICompatibleProvider(_config())
        chunks = []
        completion = None
        async for item in provider.stream(PROMPT):
            if item.is_final:
                completion = item.completion
            else:
                chunks.append(item.delta)
        assert "".join(chunks) == "hello world"
        assert completion is not None
        assert completion.text == "hello world"

    @respx.mock
    async def test_stream_without_done_marker_is_rejected(self) -> None:
        """流断了但已收到的部分不算结果——否则一次网络抖动就产出被截断的结论。"""
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200, text=_sse("partial answer", done=False),
                headers={"content-type": "text/event-stream"},
            )
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderBadResponse):
            async for _ in provider.stream(PROMPT):
                pass

    @respx.mock
    async def test_malformed_sse_line_is_rejected(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200, text="data: {not json}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )
        provider = OpenAICompatibleProvider(_config())
        with pytest.raises(ProviderBadResponse):
            async for _ in provider.stream(PROMPT):
                pass


# --------------------------------------------------------------------------
# 11. fake provider：必须能模拟每一种失败
# --------------------------------------------------------------------------

class TestFakeProvider:
    async def test_fake_can_raise_each_failure(self) -> None:
        provider = FakeProvider(script=[
            FakeTurn(raise_=ProviderTimeout(attempt=1)),
            FakeTurn(raise_=ProviderRateLimited(attempt=1, retry_after=0.0)),
            FakeTurn(raise_=ProviderAuthError(attempt=1)),
            FakeTurn(raise_=ProviderBadResponse("empty", attempt=1)),
        ])
        for expected in (ProviderTimeout, ProviderRateLimited, ProviderAuthError, ProviderBadResponse):
            with pytest.raises(expected):
                await provider.complete(PROMPT)

    async def test_fake_can_return_garbage(self) -> None:
        provider = FakeProvider(script=[FakeTurn(text="not json at all")])
        completion = await provider.complete(PROMPT)
        with pytest.raises(MalformedJsonError):
            parse_structured(completion.text, StrictDiagnosis)

    async def test_fake_can_return_fenced_json(self) -> None:
        provider = FakeProvider(script=[
            FakeTurn(text='```json\n{"root_cause":"x","confidence":"low","evidence_ids":[]}\n```')
        ])
        result = parse_structured((await provider.complete(PROMPT)).text, StrictDiagnosis)
        assert result.unwrapped is True

    async def test_fake_default_response_is_deterministic(self) -> None:
        a = FakeProvider()
        b = FakeProvider()
        first = await a.complete(PROMPT)
        second = await b.complete(PROMPT)
        assert first.text == second.text, "CI 依赖 fake 的确定性"

    async def test_fake_different_prompt_gives_different_response(self) -> None:
        provider_a = FakeProvider()
        provider_b = FakeProvider()
        a = await provider_a.complete([ChatMessage(role="user", content="prompt A")])
        b = await provider_b.complete([ChatMessage(role="user", content="prompt B")])
        assert a.text != b.text

    async def test_fake_script_exhaustion_is_explicit(self) -> None:
        provider = FakeProvider(script=[FakeTurn(text="{}")], strict_script=True)
        await provider.complete(PROMPT)
        with pytest.raises(AssertionError):
            await provider.complete(PROMPT)

    async def test_fake_records_calls_for_assertions(self) -> None:
        provider = FakeProvider()
        await provider.complete(PROMPT)
        await provider.complete(PROMPT)
        assert provider.calls_made == 2
        assert len(provider.recorded_prompts) == 2

    async def test_fake_enforces_call_limit_too(self) -> None:
        provider = FakeProvider(max_calls=1)
        await provider.complete(PROMPT)
        with pytest.raises(ProviderBudgetExceeded):
            await provider.complete(PROMPT)

    async def test_fake_can_stream(self) -> None:
        provider = FakeProvider(script=[FakeTurn(text="hello world")])
        deltas = []
        final = None
        async for item in provider.stream(PROMPT):
            if item.is_final:
                final = item.completion
            else:
                deltas.append(item.delta)
        assert "".join(deltas) == "hello world"
        assert final.text == "hello world"

    async def test_fake_stream_can_break_midway(self) -> None:
        provider = FakeProvider(script=[FakeTurn(text="partial", break_stream=True)])
        with pytest.raises(ProviderBadResponse):
            async for _ in provider.stream(PROMPT):
                pass


# --------------------------------------------------------------------------
# 12. 契约：fake 与真实 Adapter 同构
# --------------------------------------------------------------------------

class TestProviderContract:
    """防止 fake 漂移成真实 Provider 无法满足的理想化接口。"""

    @respx.mock
    async def test_both_return_same_completion_shape(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json=_envelope('{"a": 1}'))
        )
        real = await OpenAICompatibleProvider(_config()).complete(PROMPT)
        fake = await FakeProvider(script=[FakeTurn(text='{"a": 1}')]).complete(PROMPT)

        assert type(real).__name__ == type(fake).__name__
        assert set(real.model_dump().keys()) == set(fake.model_dump().keys())
        assert real.text == fake.text

    def test_both_satisfy_protocol(self) -> None:
        from agent_runtime.provider.base import ChatProvider

        assert isinstance(FakeProvider(), ChatProvider)
        assert isinstance(OpenAICompatibleProvider(_config()), ChatProvider)

    async def test_both_report_usage_and_attempts(self) -> None:
        fake = await FakeProvider().complete(PROMPT)
        assert fake.usage.total_tokens >= 0
        assert fake.attempts >= 1
        assert fake.provider_id


# --------------------------------------------------------------------------
# 13. fake 的默认响应必须满足规范 schema
# --------------------------------------------------------------------------

class TestFakeDefaultSatisfiesCanonicalSchema:
    """「默认 fake + /v1/diagnose」是最常用的路径，它必须开箱可用。

    之前 app.py 与本文件各自定义了一份 Diagnosis，字段名悄悄漂移
    （evidence vs evidence_ids），两边单测各用各的定义所以全绿，
    只有容器冒烟才暴露。这条测试把规范 schema 钉在 fake 上。
    """

    async def test_default_response_parses_against_canonical_diagnosis(self) -> None:
        completion = await FakeProvider().complete(PROMPT)
        parsed = parse_structured(completion.text, Diagnosis)
        assert parsed.value.root_cause
        assert parsed.value.recommended_next_step
        assert parsed.unwrapped is False

    async def test_default_response_has_every_canonical_field(self) -> None:
        completion = await FakeProvider().complete(PROMPT)
        payload = json.loads(completion.text)
        assert set(DIAGNOSIS_FIELDS) == set(payload), (
            "fake 的默认响应与规范 schema 的字段集必须完全一致"
        )


# --------------------------------------------------------------------------
# 14. 计价
# --------------------------------------------------------------------------

class TestPricing:
    def test_glm_flash_priced_at_list_price_not_promo(self) -> None:
        """按原价配置：促销结束后价格翻倍，按折扣价配置会让预算约束力静默减半。"""
        from agent_runtime.provider.pricing import lookup

        priced = lookup("glm-5.3-flash")
        assert priced is not None
        assert priced.prompt_micros_per_1k == 800
        assert priced.completion_micros_per_1k == 2800
        assert priced.currency == "CNY"

    def test_unknown_model_has_no_pricing(self) -> None:
        from agent_runtime.provider.pricing import lookup

        assert lookup("some-model-we-never-heard-of") is None

    def test_config_from_env_picks_up_pricing(self, monkeypatch) -> None:
        monkeypatch.setenv("RUNBOOKGUARD_LLM_BASE_URL", BASE_URL)
        monkeypatch.setenv("RUNBOOKGUARD_LLM_MODEL", "glm-5.3-flash")
        monkeypatch.setenv("RUNBOOKGUARD_LLM_API_KEY", "sk-x")
        cfg = ProviderConfig.from_env()
        assert cfg.pricing_known() is True
        assert cfg.price_per_1k_prompt_micros == 800

    def test_unknown_model_reports_pricing_unknown(self, monkeypatch) -> None:
        """成本记为 0 时必须能区分「未知」与「免费」。"""
        monkeypatch.setenv("RUNBOOKGUARD_LLM_BASE_URL", BASE_URL)
        monkeypatch.setenv("RUNBOOKGUARD_LLM_MODEL", "mystery-model")
        monkeypatch.setenv("RUNBOOKGUARD_LLM_API_KEY", "sk-x")
        cfg = ProviderConfig.from_env()
        assert cfg.pricing_known() is False
        assert cfg.price_per_1k_prompt_micros == 0

    @respx.mock
    async def test_cost_is_computed_from_usage(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json=_envelope("{}"))
        )
        # _envelope 的 usage 是 prompt=100 completion=50
        # 预期：100*800//1000 + 50*2800//1000 = 80 + 140 = 220 micros
        provider = OpenAICompatibleProvider(
            _config(price_per_1k_prompt_micros=800, price_per_1k_completion_micros=2800)
        )
        completion = await provider.complete(PROMPT)
        assert completion.cost_micros == 220

    @respx.mock
    async def test_cost_accumulates_across_calls(self) -> None:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json=_envelope("{}"))
        )
        provider = OpenAICompatibleProvider(
            _config(price_per_1k_prompt_micros=800, price_per_1k_completion_micros=2800)
        )
        await provider.complete(PROMPT)
        await provider.complete(PROMPT)
        assert provider.cost_micros_spent == 440

    def test_pricing_note_records_uncaptured_cache_discount(self) -> None:
        """缓存命中价未建模，这会高估成本——高估是安全方向，但必须写明。"""
        from agent_runtime.provider.pricing import lookup

        note = lookup("glm-5.3-flash").note
        assert "缓存" in note
        assert "高估" in note
