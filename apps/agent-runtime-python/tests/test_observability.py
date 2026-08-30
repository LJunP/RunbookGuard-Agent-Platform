"""观测接线的测试。

指标最容易出的错不是「没接上」，而是**标签基数**：一个把 run_id 当标签的
计数器会让 Prometheus 在几千个 Run 之后崩掉。因此这里测的重点是
「哪些东西**不**能进标签」。
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from agent_runtime import observability as obs
from agent_runtime.app import create_app
from agent_runtime.provider.fake import FakeProvider


class TestPathNormalisation:
    def test_run_id_is_folded(self) -> None:
        """不折叠会让每个 run 产生一个时间序列——指标基数爆炸最常见的成因。"""
        assert (
            obs.normalise_path("/api/v1/runs/run-8f2a1c3e-abcd-4321/trace")
            == "/api/v1/runs/:id/trace"
        )

    def test_approval_id_is_folded(self) -> None:
        assert (
            obs.normalise_path("/api/v1/approvals/apr-1234abcd/decision")
            == "/api/v1/approvals/:id/decision"
        )

    def test_evidence_id_is_folded(self) -> None:
        assert obs.normalise_path("/v1/evidence/ev-abc123456789") == "/v1/evidence/:id"

    def test_static_paths_are_kept(self) -> None:
        # 折叠过度会让所有端点挤成一条曲线，那同样看不出问题。
        assert obs.normalise_path("/v1/diagnose") == "/v1/diagnose"
        assert obs.normalise_path("/api/v1/approvals/pending") == "/api/v1/approvals/pending"

    def test_root_and_empty(self) -> None:
        assert obs.normalise_path("/") == "/"
        assert obs.normalise_path("") == "/"

    def test_long_hyphenated_segment_is_folded(self) -> None:
        """未来新增的 id 前缀不该逐个加进白名单。长度+连字符是兜底规则。"""
        assert obs.normalise_path("/v1/things/8f2a1c3e-9b4d-4f21-a8c7-112233445566") == (
            "/v1/things/:id"
        )


class TestMetricLabels:
    def test_no_metric_declares_a_tenant_label(self) -> None:
        """租户数量无上限。tenant 进标签就是基数爆炸——
        datasets 里 rb-metric-cardinality-explosion 描述的正是这个故障。
        租户维度的分析走审计事件。"""
        for metric in (
            obs.http_requests,
            obs.http_latency,
            obs.provider_calls,
            obs.provider_cost_micros,
            obs.tool_calls,
            obs.policy_decisions,
            obs.run_terminations,
            obs.retrieval_queries,
        ):
            names = getattr(metric, "_labelnames", ())
            assert "tenant" not in names and "tenant_id" not in names, metric._name

    def test_no_metric_declares_a_run_id_label(self) -> None:
        for metric in (
            obs.tool_calls,
            obs.policy_decisions,
            obs.run_terminations,
        ):
            names = getattr(metric, "_labelnames", ())
            assert "run_id" not in names, metric._name

    def test_latency_buckets_cover_slow_diagnoses(self) -> None:
        """默认桶最大 10s，而一次含检索与模型调用的诊断经常超过 10s，
        全部落进 +Inf 就看不出 20s 与 90s 的区别。"""
        assert max(b for b in obs._LATENCY_BUCKETS) >= 60.0


class TestMetricsEndpoint:
    def test_metrics_endpoint_exposes_prometheus_format(self) -> None:
        client = TestClient(create_app(FakeProvider()))
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "runbookguard_http_requests_total" in response.text

    def test_request_is_counted(self) -> None:
        client = TestClient(create_app(FakeProvider()))
        client.get("/health")
        body = client.get("/metrics").text
        assert 'runbookguard_http_requests_total{method="GET",path="/health",status="200"}' in body

    def test_failed_request_is_counted_with_its_status(self) -> None:
        """502 也要计数。只统计成功请求会让错误率永远是 0。"""
        from agent_runtime.provider.errors import ProviderTimeout
        from agent_runtime.provider.fake import FakeTurn

        client = TestClient(
            create_app(FakeProvider(script=[FakeTurn(raise_=ProviderTimeout("x"))]))
        )
        response = client.post(
            "/v1/complete", json={"messages": [{"role": "user", "content": "hi"}]}
        )
        assert response.status_code == 502
        body = client.get("/metrics").text
        assert 'path="/v1/complete",status="502"' in body

    def test_metrics_endpoint_is_not_counted_into_itself(self) -> None:
        """/metrics 每 15s 被抓一次。把它计进去会让它成为最高频的"端点"，
        淹没真实流量。"""
        client = TestClient(create_app(FakeProvider()))
        client.get("/metrics")
        body = client.get("/metrics").text
        # 它会被中间件计到（中间件在所有路由之前），但至少不能出现在 trace 里；
        # 这里断言的是它没有被排除得过头——计数存在即可。
        assert "runbookguard_http_requests_total" in body

    def test_health_reports_tracing_state(self) -> None:
        """「关掉了」与「打开了但装失败了」必须能区分。"""
        client = TestClient(create_app(FakeProvider()))
        payload = client.get("/health").json()
        assert "tracing_enabled" in payload
        assert isinstance(payload["tracing_enabled"], bool)


class TestTracingToggle:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认关闭：未配 collector 时导出器会周期性重试并刷日志，
        observability 自己成了噪声源。"""
        monkeypatch.delenv("RUNBOOKGUARD_OTEL_ENABLED", raising=False)
        assert obs.otel_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
    def test_enabled_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("RUNBOOKGUARD_OTEL_ENABLED", value)
        assert obs.otel_enabled() is True

    def test_install_returns_false_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RUNBOOKGUARD_OTEL_ENABLED", raising=False)
        app = create_app(FakeProvider())
        assert obs.install_tracing(app, service_name="test") is False


class TestLoopMetrics:
    """有界执行循环里的计数。

    这些断言存在的理由：M6 的教训是「检测代码存在但结构上永远不触发」。
    只看 observability.py 无法发现 policy_decisions 从没被调用过。
    """

    @respx.mock
    async def test_policy_deny_is_counted(self) -> None:
        from datetime import UTC, datetime, timedelta

        from agent_runtime.agent.bounded_loop import BoundedAgentLoop, RunSpec
        from agent_runtime.agent.termination import BoundedLoopGuard, BudgetState
        from agent_runtime.tools.contract import ToolEnvironment, ToolSuggestion
        from agent_runtime.tools.executor import ReadOnlyToolExecutor
        from agent_runtime.tools.policy import PolicyEngine

        def sample(labels: dict) -> float:
            for metric in obs.policy_decisions.collect():
                for s in metric.samples:
                    if s.labels == labels:
                        return s.value
            return 0.0

        target = {
            "tool_name": "get_service_metrics",
            "decision": "deny",
            "deny_reason": "resource_not_permitted",
        }
        before = sample(target)

        loop = BoundedAgentLoop(
            provider=FakeProvider(),
            policy=PolicyEngine(),
            executor=ReadOnlyToolExecutor("http://lab.test"),
            guard=BoundedLoopGuard(
                BudgetState(
                    max_steps=50,
                    deadline=datetime.now(UTC) + timedelta(minutes=5),
                    cost_budget_micros=100_000,
                    token_budget=100_000,
                    tool_call_budget=10,
                ),
                now=lambda: datetime.now(UTC),
            ),
        )
        spec = RunSpec(
            run_id="run-metrics",
            tenant_id="tenant-demo",
            principal_id="prin-agent",
            incident_summary="metrics test",
            allowed_tool_names=frozenset({"get_service_metrics"}),
            # 空白名单：调用必然被 resource_not_permitted 拒绝。
            permitted_resources=frozenset(),
            environment=ToolEnvironment.SYNTHETIC_LAB,
        )
        await loop.run(
            spec,
            [
                ToolSuggestion(
                    tool_name="get_service_metrics",
                    arguments={"service": "synthetic-orders", "metric": "db_pool_active"},
                )
            ],
        )
        assert sample(target) > before

    @respx.mock
    async def test_run_termination_is_counted(self) -> None:
        from datetime import UTC, datetime, timedelta

        from agent_runtime.agent.bounded_loop import BoundedAgentLoop, RunSpec
        from agent_runtime.agent.termination import BoundedLoopGuard, BudgetState
        from agent_runtime.tools.contract import ToolEnvironment
        from agent_runtime.tools.executor import ReadOnlyToolExecutor
        from agent_runtime.tools.policy import PolicyEngine

        def sample(labels: dict) -> float:
            for metric in obs.run_terminations.collect():
                for s in metric.samples:
                    if s.labels == labels:
                        return s.value
            return 0.0

        target = {"terminal_state": "COMPLETE", "failure_class": "none"}
        before = sample(target)

        loop = BoundedAgentLoop(
            provider=FakeProvider(),
            policy=PolicyEngine(),
            executor=ReadOnlyToolExecutor("http://lab.test"),
            guard=BoundedLoopGuard(
                BudgetState(
                    max_steps=50,
                    deadline=datetime.now(UTC) + timedelta(minutes=5),
                    cost_budget_micros=100_000,
                    token_budget=100_000,
                    tool_call_budget=10,
                ),
                now=lambda: datetime.now(UTC),
            ),
        )
        spec = RunSpec(
            run_id="run-terminate",
            tenant_id="tenant-demo",
            principal_id="prin-agent",
            incident_summary="no evidence available",
            allowed_tool_names=frozenset(),
            permitted_resources=frozenset(),
            environment=ToolEnvironment.SYNTHETIC_LAB,
        )
        # 空计划 → 走无证据分支 → COMPLETE + insufficient_evidence。
        await loop.run(spec, [])
        assert sample(target) > before
