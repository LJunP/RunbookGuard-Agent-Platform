"""Agent Runtime 的 FastAPI 骨架（M3）。

这一层目前只暴露 Provider 能力，不含任何 Agent 编排——编排是 M4。
刻意保持薄：M3 要证明的是「模型失败不产生假成功」，多加一层业务逻辑只会
让那个判据变模糊。
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import observability as obs
from .provider.base import ChatProvider
from .provider.errors import ProviderError, StructuredOutputError
from .provider.factory import ProviderConfigurationError, build_provider
from .provider.models import ChatMessage
from .provider.structured import parse_structured
from .redaction import redact
from .schemas import Diagnosis

log = logging.getLogger(__name__)


class CompleteRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)


class StructuredRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)


def create_app(provider: ChatProvider | None = None) -> FastAPI:
    app = FastAPI(title="RunbookGuard Agent Runtime", version="0.1.0")
    app.state.provider = provider or build_provider()
    app.state.tracing_enabled = obs.install_tracing(app, service_name="agent-runtime")
    if obs.otel_enabled() and not app.state.tracing_enabled:
        # 请求了 trace 却没装上，必须说出来：静默降级会让「trace 里什么都没有」
        # 被误判成「没有请求经过」。
        log.warning("OTel tracing was requested but could not be installed")

    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        import time

        path = obs.normalise_path(request.url.path)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 未处理异常也要计数，否则 5xx 率会漏掉最严重的那一类。
            obs.http_requests.labels(request.method, path, "500").inc()
            obs.http_latency.labels(request.method, path).observe(
                time.perf_counter() - start
            )
            raise
        obs.http_requests.labels(request.method, path, str(response.status_code)).inc()
        obs.http_latency.labels(request.method, path).observe(time.perf_counter() - start)
        return response

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "message": str(exc.errors())},
        )

    @app.exception_handler(ProviderError)
    async def on_provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        """Provider 失败映射为 502 而非 500。

        502 的语义是「上游坏了」，与 failure_class 一起返回，让调用方能区分
        「该重试」和「该放弃」——这是「模型失败不产生假成功」在 HTTP 层的落点。
        """
        return JSONResponse(
            status_code=502,
            content={
                "error": type(exc).__name__,
                "failure_class": exc.failure_class,
                "retriable": exc.retriable,
                "attempt": exc.attempt,
                "message": redact(str(exc)),
            },
        )

    @app.exception_handler(StructuredOutputError)
    async def on_structured_error(_: Request, exc: StructuredOutputError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": type(exc).__name__,
                "failure_class": exc.failure_class,
                "retriable": exc.retriable,
                "message": redact(str(exc)),
            },
        )

    @app.exception_handler(ProviderConfigurationError)
    async def on_config_error(_: Request, exc: ProviderConfigurationError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": "provider_not_configured", "message": redact(str(exc))},
        )

    @app.get("/health")
    def health() -> dict:
        provider = app.state.provider
        return {
            "status": "ok",
            "provider_id": provider.provider_id,
            "calls_made": provider.calls_made,
            # trace 是否真的接上了要能查到。只看配置无法区分
            # 「关掉了」与「打开了但装失败了」。
            "tracing_enabled": app.state.tracing_enabled,
        }

    @app.get("/metrics")
    def metrics() -> Response:
        payload, content_type = obs.render_metrics()
        return Response(content=payload, media_type=content_type)

    @app.post("/v1/complete")
    async def complete(request: CompleteRequest) -> dict:
        completion = await app.state.provider.complete(request.messages)
        return {
            "text": completion.text,
            "model": completion.model,
            "provider_id": completion.provider_id,
            "finish_reason": completion.finish_reason,
            "attempts": completion.attempts,
            "usage": completion.usage.model_dump(),
            "cost_micros": completion.cost_micros,
        }

    @app.post("/v1/diagnose")
    async def diagnose(request: StructuredRequest) -> dict:
        """结构化输出。schema 不符时返回 502 + typed failure，绝不静默修补。"""
        completion = await app.state.provider.complete(request.messages)
        parsed = parse_structured(completion.text, Diagnosis)
        return {
            "diagnosis": parsed.value.model_dump(),
            # 解包被显式暴露给调用方，使「剥离了代码围栏」这件事可审计。
            "unwrapped": parsed.unwrapped,
            "usage": completion.usage.model_dump(),
            "attempts": completion.attempts,
        }

    @app.post("/v1/complete/stream")
    async def complete_stream(request: CompleteRequest) -> StreamingResponse:
        async def event_stream():
            try:
                async for item in app.state.provider.stream(request.messages):
                    if item.is_final:
                        payload = {
                            "type": "final",
                            "text": item.completion.text,
                            "usage": item.completion.usage.model_dump(),
                            "finish_reason": item.completion.finish_reason,
                        }
                    else:
                        payload = {"type": "delta", "delta": item.delta}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except (ProviderError, StructuredOutputError) as exc:
                # 流已经开始后无法改状态码，因此把失败作为一个 error 事件送出，
                # 且**不发 [DONE]**——消费者据此判定这不是一个完整结果。
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "error",
                            "error": type(exc).__name__,
                            "failure_class": exc.failure_class,
                            "retriable": getattr(exc, "retriable", False),
                            "message": redact(str(exc)),
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app
