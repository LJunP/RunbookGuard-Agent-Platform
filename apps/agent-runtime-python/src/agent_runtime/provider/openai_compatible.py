"""OpenAI-compatible Chat Completions 适配器。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from ..redaction import redact
from .errors import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderBudgetExceeded,
    ProviderContentFiltered,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from .models import ChatMessage, Completion, ProviderConfig, StreamItem, Usage

log = logging.getLogger(__name__)

_DONE_MARKER = "[DONE]"


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.provider_id = config.provider_id
        self.calls_made = 0
        self.cost_micros_spent = 0
        self._client = client

    # -- 公开接口 ---------------------------------------------------------

    async def complete(self, messages: list[ChatMessage]) -> Completion:
        last_error: ProviderError | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            self._guard_budget(attempt)
            try:
                return await self._attempt_once(messages, attempt)
            except ProviderError as exc:
                last_error = exc
                if not exc.retriable or attempt == self.config.max_attempts:
                    raise
                await self._sleep_before_retry(attempt, exc)
        raise last_error if last_error else ProviderBadResponse("no attempt was made")

    async def stream(self, messages: list[ChatMessage]) -> AsyncIterator[StreamItem]:
        """流式不做重试：已经吐出去的分块无法撤回，重试会让消费者看到重复内容。"""
        self._guard_budget(1)
        self.calls_made += 1
        payload = self._request_payload(messages, stream=True)
        collected: list[str] = []
        finish_reason: str | None = None
        saw_done = False
        usage = Usage()

        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        owns_client = self._client is None
        try:
            async with client.stream(
                "POST", self._url(), json=payload, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    self._raise_for_status(response.status_code, body.decode(errors="replace"), 1)

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == _DONE_MARKER:
                        saw_done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderBadResponse(
                            f"malformed SSE payload: {exc.msg}", attempt=1
                        ) from exc
                    delta, reason, chunk_usage = self._read_chunk(chunk)
                    if chunk_usage is not None:
                        usage = chunk_usage
                    if reason:
                        finish_reason = reason
                    if delta:
                        collected.append(delta)
                        yield StreamItem(delta=delta)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(str(exc), attempt=1) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(redact(str(exc)) or "transport error", attempt=1) from exc
        finally:
            if owns_client:
                await client.aclose()

        if not saw_done:
            # 流断了但已收到的部分不算结果——否则一次网络抖动就产出被截断的诊断结论。
            raise ProviderBadResponse(
                "stream ended without terminator; partial output is not a result", attempt=1
            )
        text = "".join(collected)
        if not text:
            raise ProviderBadResponse("stream produced no content", attempt=1)
        if finish_reason == "content_filter":
            raise ProviderContentFiltered(attempt=1)
        if finish_reason == "length":
            raise ProviderBadResponse("response truncated (finish_reason=length)", attempt=1)

        completion = self._build_completion(text, usage, finish_reason or "stop", 1)
        yield StreamItem(is_final=True, completion=completion)

    # -- 内部实现 ---------------------------------------------------------

    async def _attempt_once(self, messages: list[ChatMessage], attempt: int) -> Completion:
        self.calls_made += 1
        payload = self._request_payload(messages, stream=False)
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        owns_client = self._client is None
        try:
            response = await client.post(self._url(), json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(str(exc), attempt=attempt) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(redact(str(exc)) or "transport error", attempt=attempt) from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code >= 400:
            self._raise_for_status(response.status_code, response.text, attempt, response.headers)

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderBadResponse(
                f"response body is not JSON: {redact(response.text[:200])}", attempt=attempt
            ) from exc

        return self._parse_envelope(body, attempt)

    def _parse_envelope(self, body: Any, attempt: int) -> Completion:
        if not isinstance(body, dict):
            raise ProviderBadResponse("response envelope is not an object", attempt=attempt)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderBadResponse("response contains no choices", attempt=attempt)

        choice = choices[0]
        finish_reason = str(choice.get("finish_reason") or "stop")
        if finish_reason == "content_filter":
            raise ProviderContentFiltered(attempt=attempt)

        message = choice.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderBadResponse("response content is empty", attempt=attempt)

        # 截断的响应是 200 但内容不完整——这正是 Gate 条件要防的假成功。
        if finish_reason == "length":
            raise ProviderBadResponse(
                "response truncated (finish_reason=length)", attempt=attempt
            )

        usage = self._read_usage(body.get("usage"))
        return self._build_completion(text, usage, finish_reason, attempt)

    def _build_completion(
        self, text: str, usage: Usage, finish_reason: str, attempt: int
    ) -> Completion:
        cost = self._cost_micros(usage)
        self.cost_micros_spent += cost
        return Completion(
            text=text,
            usage=usage,
            model=self.config.model,
            provider_id=self.provider_id,
            finish_reason=finish_reason,
            attempts=attempt,
            cost_micros=cost,
        )

    @staticmethod
    def _read_usage(raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        return Usage(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
        )

    @staticmethod
    def _read_chunk(chunk: Any) -> tuple[str, str | None, Usage | None]:
        if not isinstance(chunk, dict):
            raise ProviderBadResponse("SSE chunk is not an object", attempt=1)
        choices = chunk.get("choices") or []
        delta_text = ""
        finish_reason = None
        if choices:
            delta = choices[0].get("delta") or {}
            delta_text = delta.get("content") or ""
            finish_reason = choices[0].get("finish_reason")
        usage_raw = chunk.get("usage")
        usage = OpenAICompatibleProvider._read_usage(usage_raw) if usage_raw else None
        return delta_text, finish_reason, usage

    def _cost_micros(self, usage: Usage) -> int:
        # 价格未配置时成本记为 0，并在报告中标注为未知——不猜测价格。
        prompt_cost = usage.prompt_tokens * self.config.price_per_1k_prompt_micros // 1000
        completion_cost = (
            usage.completion_tokens * self.config.price_per_1k_completion_micros // 1000
        )
        return prompt_cost + completion_cost

    def _guard_budget(self, attempt: int) -> None:
        if self.calls_made >= self.config.max_calls:
            raise ProviderBudgetExceeded(
                f"provider call limit reached ({self.config.max_calls}); refusing further calls",
                attempt=attempt,
            )
        if self.config.max_cost_micros and self.cost_micros_spent >= self.config.max_cost_micros:
            raise ProviderBudgetExceeded(
                f"provider cost limit reached ({self.config.max_cost_micros} micros)",
                attempt=attempt,
            )

    async def _sleep_before_retry(self, attempt: int, error: ProviderError) -> None:
        if isinstance(error, ProviderRateLimited) and error.retry_after is not None:
            await asyncio.sleep(error.retry_after)
            return
        backoff = self.config.backoff_seconds
        delay = backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0.0
        if delay:
            await asyncio.sleep(delay)

    def _raise_for_status(
        self, status: int, body: str, attempt: int, headers: httpx.Headers | None = None
    ) -> None:
        detail = redact(body[:300]) or ""
        if status in (401, 403):
            raise ProviderAuthError(
                f"provider rejected credentials (HTTP {status}): {detail}", attempt=attempt
            )
        if status == 429:
            retry_after = None
            if headers is not None and "retry-after" in headers:
                try:
                    retry_after = float(headers["retry-after"])
                except ValueError:
                    retry_after = None
            raise ProviderRateLimited(
                f"provider rate limited (HTTP 429): {detail}",
                attempt=attempt,
                retry_after=retry_after,
            )
        if status >= 500:
            raise ProviderUnavailable(
                f"provider unavailable (HTTP {status}): {detail}", attempt=attempt
            )
        raise ProviderBadResponse(
            f"provider rejected the request (HTTP {status}): {detail}", attempt=attempt
        )

    def _url(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _request_payload(self, messages: list[ChatMessage], *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.model_dump() for m in messages],
            "stream": stream,
        }
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload
