"""Python 侧的观测接入（M7）。

三个刻意的选择：

1. **默认关闭 trace 导出。** 未配 collector 时 OTLP 导出器会周期性重试并刷日志——
   observability 自己成了噪声源。开关是 `RUNBOOKGUARD_OTEL_ENABLED`。

2. **指标不带 tenant 标签。** 租户数量无上限，会造成指标基数爆炸
   （datasets 里 `rb-metric-cardinality-explosion` 描述的正是这个故障）。
   租户维度的分析走审计事件，不走 Prometheus。

3. **failure_class 是标签，failure 的自由文本不是。** 前者是封闭枚举（15 个值），
   后者无界。把 message 当标签会让基数随日志内容增长。
"""

from __future__ import annotations

import os

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# 桶按「人对延迟的感知」切，不用默认桶：默认桶最大 10s，而一次含检索与模型调用的
# 诊断经常超过 10s，全部落进 +Inf 就看不出 20s 与 90s 的区别。
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)

http_requests = Counter(
    "runbookguard_http_requests_total",
    "HTTP requests handled by the agent runtime.",
    ("method", "path", "status"),
)

http_latency = Histogram(
    "runbookguard_http_request_duration_seconds",
    "HTTP request duration.",
    ("method", "path"),
    buckets=_LATENCY_BUCKETS,
)

provider_calls = Counter(
    "runbookguard_provider_calls_total",
    "Model provider calls, by outcome.",
    ("provider_id", "model", "outcome"),
)

provider_cost_micros = Counter(
    "runbookguard_provider_cost_micros_total",
    "Accumulated model cost in micros (list price).",
    ("provider_id", "model"),
)

tool_calls = Counter(
    "runbookguard_tool_calls_total",
    "Tool executions, by tool and outcome.",
    ("tool_name", "outcome"),
)

policy_decisions = Counter(
    "runbookguard_policy_decisions_total",
    "Policy decisions. deny_reason is a closed enum; never free text.",
    ("tool_name", "decision", "deny_reason"),
)

run_terminations = Counter(
    "runbookguard_run_terminations_total",
    "Run terminations by terminal state and failure class.",
    ("terminal_state", "failure_class"),
)

retrieval_queries = Counter(
    "runbookguard_retrieval_queries_total",
    "Runbook retrieval queries, by whether anything matched.",
    ("hit",),
)

checkpoint_metadata_failures = Counter(
    "runbookguard_checkpoint_metadata_failures_total",
    "Failed checkpoint metadata uploads to the control plane. "
    "静默丢元数据会让 digest 核对变成摆设，因此失败必须可见。",
)

retrieval_latency = Histogram(
    "runbookguard_retrieval_duration_seconds",
    "Runbook retrieval duration.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

retrieval_stage_latency = Histogram(
    "runbookguard_retrieval_stage_duration_seconds",
    "Per-sub-retriever latency inside the hybrid pipeline (lexical / vector). "
    "总延迟只回答「检索慢」，这一组回答「哪一段慢」。",
    ("stage",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def otel_enabled() -> bool:
    return os.environ.get("RUNBOOKGUARD_OTEL_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def install_tracing(app, *, service_name: str) -> bool:
    """接入 OTLP trace 导出。返回是否真的启用了。

    失败时返回 False 而不是抛异常：观测不可用不该让服务起不来。
    但也不静默——调用方会把结果写进日志与 /health。
    """
    if not otel_enabled():
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318"
        ).rstrip("/")
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": service_name,
                    "service.namespace": "runbookguard",
                    "deployment.environment": os.environ.get(
                        "RUNBOOKGUARD_ENV", "local"
                    ),
                }
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
        )
        trace.set_tracer_provider(provider)
        # /health 与 /metrics 不入 trace：它们每 15s 被抓一次，会把真实请求淹没。
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls="health,metrics", tracer_provider=provider
        )
        # 出站 HTTP 也要 instrument，否则「调 synthetic-lab 花了多久」不在 trace 里，
        # 而那恰恰是排查工具超时最需要的一段。
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
        return True
    except Exception:  # noqa: BLE001
        return False


def normalise_path(path: str) -> str:
    """把路径里的 id 段折叠掉。

    不折叠会让 `/api/v1/runs/{uuid}/trace` 每个 run 产生一个时间序列——
    这是指标基数爆炸最常见的成因。
    """
    parts = []
    for segment in path.split("/"):
        if not segment:
            continue
        if _looks_like_id(segment):
            parts.append(":id")
        else:
            parts.append(segment)
    return "/" + "/".join(parts) if parts else "/"


def _looks_like_id(segment: str) -> bool:
    if len(segment) >= 24 and "-" in segment:
        return True
    return segment.startswith(("run-", "apr-", "inc-", "eval-", "stp-", "ev-"))
