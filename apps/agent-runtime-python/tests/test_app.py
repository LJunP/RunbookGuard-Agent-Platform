"""HTTP 层的失败映射测试。

重点不是「成功路径返回 200」，而是「每种模型失败在 HTTP 层都表现为可识别的失败」。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_runtime.app import create_app
from agent_runtime.provider.errors import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderContentFiltered,
    ProviderRateLimited,
    ProviderTimeout,
)
from agent_runtime.provider.fake import FakeProvider, FakeTurn

VALID = json.dumps(
    {
        "conclusion_type": "diagnosis",
        "root_cause": "connection pool exhausted after v1.5.0",
        "confidence": "high",
        "root_cause_service": "synthetic-orders",
        "claims": [
            {
                "statement": "the pool saturated shortly after the v1.5.0 deploy",
                "evidence_ids": ["ev-1", "ev-2"],
            }
        ],
        "ruled_out": [],
        "conflicting_signals": [],
        "missing_evidence": [],
        "proposed_action": None,
        "recommended_next_step": "roll back to v1.4.2",
    }
)

MESSAGES = {"messages": [{"role": "user", "content": "diagnose"}]}


def client_with(*turns: FakeTurn) -> TestClient:
    return TestClient(create_app(FakeProvider(script=list(turns))))


class TestHealthAndHappyPath:
    def test_health_reports_provider(self) -> None:
        with client_with() as c:
            body = c.get("/health").json()
            assert body["status"] == "ok"
            assert body["provider_id"] == "fake"

    def test_complete_returns_usage_and_attempts(self) -> None:
        with client_with(FakeTurn(text="hello")) as c:
            body = c.post("/v1/complete", json=MESSAGES).json()
            assert body["text"] == "hello"
            assert body["attempts"] == 1
            assert "usage" in body

    def test_diagnose_returns_structured_payload(self) -> None:
        with client_with(FakeTurn(text=VALID)) as c:
            body = c.post("/v1/diagnose", json=MESSAGES).json()
            assert body["diagnosis"]["confidence"] == "high"
            assert body["unwrapped"] is False


class TestFailureMapping:
    """每种失败都必须是可识别的失败，不能是 200。"""

    def test_timeout_maps_to_502_with_failure_class(self) -> None:
        with client_with(FakeTurn(raise_=ProviderTimeout(attempt=3))) as c:
            r = c.post("/v1/complete", json=MESSAGES)
            assert r.status_code == 502
            body = r.json()
            assert body["error"] == "ProviderTimeout"
            assert body["failure_class"] == "provider_failure"
            assert body["retriable"] is True
            assert body["attempt"] == 3

    def test_rate_limit_maps_to_502_retriable(self) -> None:
        with client_with(FakeTurn(raise_=ProviderRateLimited(retry_after=1.0))) as c:
            body = c.post("/v1/complete", json=MESSAGES).json()
            assert body["retriable"] is True

    def test_auth_error_is_not_retriable(self) -> None:
        with client_with(FakeTurn(raise_=ProviderAuthError())) as c:
            body = c.post("/v1/complete", json=MESSAGES).json()
            assert body["failure_class"] == "authorization_failed"
            assert body["retriable"] is False

    def test_content_filter_maps_to_safety_rule(self) -> None:
        with client_with(FakeTurn(raise_=ProviderContentFiltered())) as c:
            body = c.post("/v1/complete", json=MESSAGES).json()
            assert body["failure_class"] == "safety_rule_triggered"

    def test_bad_response_is_not_retriable(self) -> None:
        with client_with(FakeTurn(raise_=ProviderBadResponse("empty"))) as c:
            body = c.post("/v1/complete", json=MESSAGES).json()
            assert body["retriable"] is False

    def test_garbage_output_does_not_become_200(self) -> None:
        """最重要的一条：模型胡说不能表现为成功。"""
        with client_with(FakeTurn(text="the database is probably fine")) as c:
            r = c.post("/v1/diagnose", json=MESSAGES)
            assert r.status_code == 502
            assert r.json()["error"] == "MalformedJsonError"

    def test_schema_violation_does_not_become_200(self) -> None:
        # 缺 conclusion_type / claims 等必填字段。
        with client_with(FakeTurn(text='{"root_cause": "x"}')) as c:
            r = c.post("/v1/diagnose", json=MESSAGES)
            assert r.status_code == 502
            assert r.json()["error"] == "SchemaViolationError"

    def test_truncated_json_does_not_become_200(self) -> None:
        with client_with(FakeTurn(text='{"root_cause": "poo')) as c:
            assert c.post("/v1/diagnose", json=MESSAGES).status_code == 502

    def test_empty_messages_rejected(self) -> None:
        with client_with() as c:
            assert c.post("/v1/diagnose", json={"messages": []}).status_code == 422


class TestUnwrapVisibility:
    def test_fenced_output_is_flagged_to_caller(self) -> None:
        """解包必须对调用方可见，否则「不许静默修补」就没落实。"""
        with client_with(FakeTurn(text=f"```json\n{VALID}\n```")) as c:
            body = c.post("/v1/diagnose", json=MESSAGES).json()
            assert body["unwrapped"] is True
            assert body["diagnosis"]["confidence"] == "high"


class TestStreamingEndpoint:
    def test_stream_emits_deltas_then_final_then_done(self) -> None:
        with client_with(FakeTurn(text="alpha beta gamma")) as c:
            with c.stream("POST", "/v1/complete/stream", json=MESSAGES) as r:
                events = [
                    line[len("data: "):]
                    for line in r.iter_lines()
                    if line.startswith("data: ")
                ]
        assert events[-1] == "[DONE]"
        parsed = [json.loads(e) for e in events[:-1]]
        assert [p["type"] for p in parsed].count("delta") == 3
        assert parsed[-1]["type"] == "final"
        assert parsed[-1]["text"] == "alpha beta gamma"

    def test_broken_stream_emits_error_and_no_done(self) -> None:
        """流失败时不发 [DONE]，消费者据此判定这不是完整结果。"""
        with client_with(FakeTurn(text="partial", break_stream=True)) as c:
            with c.stream("POST", "/v1/complete/stream", json=MESSAGES) as r:
                events = [
                    line[len("data: "):]
                    for line in r.iter_lines()
                    if line.startswith("data: ")
                ]
        assert "[DONE]" not in events
        last = json.loads(events[-1])
        assert last["type"] == "error"
        assert last["error"] == "ProviderBadResponse"


class TestSecretHandling:
    def test_error_body_is_redacted(self) -> None:
        leaky = ProviderAuthError("rejected key sk-abcdefghijklmnopqrstuvwx")
        with client_with(FakeTurn(raise_=leaky)) as c:
            body = c.post("/v1/complete", json=MESSAGES).json()
            assert "sk-abcdefghijklmnopqrstuvwx" not in json.dumps(body)


class TestProviderFactory:
    def test_default_is_fake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RUNBOOKGUARD_LLM_PROVIDER", raising=False)
        from agent_runtime.provider.factory import build_provider

        assert build_provider().provider_id == "fake"

    def test_real_provider_without_config_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不静默回退到 fake——那会让「以为在调真实模型」的验证变成自欺。"""
        from agent_runtime.provider.factory import (
            ProviderConfigurationError,
            build_provider,
        )

        for name in ("BASE_URL", "MODEL", "API_KEY"):
            monkeypatch.delenv(f"RUNBOOKGUARD_LLM_{name}", raising=False)
        with pytest.raises(ProviderConfigurationError) as exc:
            build_provider(kind="openai-compatible")
        assert "RUNBOOKGUARD_LLM_BASE_URL" in str(exc.value)

    def test_unknown_kind_raises(self) -> None:
        from agent_runtime.provider.factory import (
            ProviderConfigurationError,
            build_provider,
        )

        with pytest.raises(ProviderConfigurationError):
            build_provider(kind="anthropic-native")
