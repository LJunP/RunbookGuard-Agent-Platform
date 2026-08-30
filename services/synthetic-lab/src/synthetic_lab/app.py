"""Synthetic Lab HTTP 接口。

它是只读工具的数据来源（M0 §7），也是不可信数据源的模拟器：
注入载荷原样返回，处理责任在 Agent Runtime 侧。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, Field

_requests = Counter(
    "synthetic_lab_requests_total",
    "Requests served by the synthetic lab.",
    ("method", "path", "status"),
)

from . import generator
from .actions import ActionLedger, ActionRecord, ActionRejected
from .generator import ActiveScenario
from .scenarios import Scenario, ScenarioLibrary


class RestartRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=64)


class RollbackRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=64)
    target_version: str = Field(min_length=1, max_length=64)


class ThrottleRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=64)
    # 整数基点而非浮点百分比：ADR-0002 禁止浮点参与摘要。
    rate_basis_points: int = Field(ge=0, le=10000)


def _action_response(record: ActionRecord) -> dict:
    return {
        "idempotency_key": record.idempotency_key,
        "action": record.action,
        "service": record.service,
        "detail": record.detail,
        "performed_at": record.performed_at,
        # replayed=True 表示这个键之前提交过，副作用没有再次发生。
        "replayed": record.replayed,
    }


class ScenarioAlreadyActive(Exception):
    def __init__(self, scenario_id: str) -> None:
        super().__init__(scenario_id)
        self.scenario_id = scenario_id


class ServiceAlreadyUnderScenario(Exception):
    def __init__(self, services: list[str], holder: str) -> None:
        super().__init__(", ".join(services))
        self.services = services
        self.holder = holder


class LabState:
    """当前激活的剧本。

    拒绝同一服务被两个剧本同时作用（ADR-0004 §6）：混合特征没有唯一正确答案，
    让 Agent 诊断一个我自己都说不清的输入，得到的成功率数字没有意义。
    """

    def __init__(self, library: ScenarioLibrary) -> None:
        self.library = library
        self.active: dict[str, ActiveScenario] = {}

    def start(self, scenario: Scenario, now: datetime) -> ActiveScenario:
        if scenario.id in self.active:
            raise ScenarioAlreadyActive(scenario.id)
        holder_by_service = {
            svc: sid
            for sid, act in self.active.items()
            for svc in act.scenario.services_touched()
        }
        clash = sorted(scenario.services_touched() & set(holder_by_service))
        if clash:
            raise ServiceAlreadyUnderScenario(clash, holder_by_service[clash[0]])
        entry = ActiveScenario(scenario=scenario, t0=now)
        self.active[scenario.id] = entry
        return entry

    def stop_all(self) -> int:
        count = len(self.active)
        self.active.clear()
        return count

    def for_service(self, service: str) -> ActiveScenario | None:
        for entry in self.active.values():
            if service in entry.scenario.services:
                return entry
        return None

    def for_queue(self, queue: str) -> ActiveScenario | None:
        for entry in self.active.values():
            if queue in entry.scenario.queues:
                return entry
        return None


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _aligned_now() -> datetime:
    """T0 对齐到整分钟。

    日志按「每分钟 N 行」生成，行内的秒偏移由 seed 决定。若 T0 带秒数，
    查询窗口的两端就会切在分钟中间，落在切口外的那几行被丢弃——启动时刻的
    秒数因此渗进输出，同一剧本连续跑三次会得到不同的日志条数。
    """
    return datetime.now(UTC).replace(second=0, microsecond=0)


def create_app(library: ScenarioLibrary) -> FastAPI:
    app = FastAPI(title="RunbookGuard Synthetic Lab", version="0.1.0")
    state = LabState(library)
    ledger = ActionLedger()
    app.state.lab = state
    app.state.ledger = ledger

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        """lab 自身的指标。

        它暴露的是「lab 被调用了多少次」，不是剧本里那些合成指标——
        后者走 /v1/metrics，是给 Agent 当证据的数据，两者不能混。
        混在一起会让 Prometheus 里出现合成故障的假指标，把真实的部署指标污染掉。
        """
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        path = request.url.path
        # 剧本 id 折叠掉，否则每个剧本一个时间序列。
        if path.startswith("/v1/scenarios/") and path.count("/") >= 3:
            path = "/v1/scenarios/:id" + ("/start" if path.endswith("/start") else "")
        response = await call_next(request)
        _requests.labels(request.method, path, str(response.status_code)).inc()
        return response

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 非法参数一律拒绝，不静默使用默认值（ADR-0004 §验证方式 3）。
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_parameters", "message": str(exc.errors())},
        )

    @app.exception_handler(ScenarioAlreadyActive)
    async def on_already_active(_: Request, exc: ScenarioAlreadyActive) -> JSONResponse:
        return _error(409, "scenario_already_active", f"scenario {exc.scenario_id} is already running")

    @app.exception_handler(ServiceAlreadyUnderScenario)
    async def on_service_clash(_: Request, exc: ServiceAlreadyUnderScenario) -> JSONResponse:
        return _error(
            409,
            "service_already_under_scenario",
            f"services {exc.services} already affected by scenario {exc.holder}",
        )

    def resolve_service(service: str) -> ActiveScenario:
        """返回该服务当前的剧本，或基线模式的合成剧本。

        未启动剧本时返回基线而非空/错误：只读工具在没有故障时也必须能查到数据，
        否则 Agent 无法区分「系统健康」与「数据源坏了」。
        """
        entry = state.for_service(service)
        if entry is not None:
            return entry
        for scenario in library.all():
            if service in scenario.services:
                return ActiveScenario(scenario=scenario, t0=_baseline_t0(), baseline_only=True)
        raise KeyError(service)

    def resolve_queue(queue: str) -> ActiveScenario:
        entry = state.for_queue(queue)
        if entry is not None:
            return entry
        for scenario in library.all():
            if queue in scenario.queues:
                return ActiveScenario(scenario=scenario, t0=_baseline_t0(), baseline_only=True)
        raise KeyError(queue)

    def _baseline_t0() -> datetime:
        return _aligned_now()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "scenarios": len(library.all()), "active": len(state.active)}

    @app.get("/v1/scenarios")
    def list_scenarios() -> dict:
        return {
            "scenarios": [
                {
                    "id": s.id,
                    "version": s.version,
                    "description": s.description,
                    "seed": s.seed,
                    "fingerprint": s.fingerprint(),
                    "services": sorted(s.services),
                    "queues": sorted(s.queues),
                    "baseline_minutes": s.baseline_minutes,
                    "incident_minutes": s.incident_minutes,
                }
                for s in library.all()
            ]
        }

    @app.get("/v1/scenarios/active")
    def active_scenarios() -> dict:
        return {
            "active": [
                {"id": sid, "t0": entry.t0.isoformat(), "services": sorted(entry.scenario.services)}
                for sid, entry in sorted(state.active.items())
            ]
        }

    @app.post("/v1/scenarios/{scenario_id}/start")
    def start_scenario(scenario_id: str) -> JSONResponse:
        try:
            scenario = library.get(scenario_id)
        except KeyError:
            return _error(404, "scenario_not_found", f"unknown scenario {scenario_id}")
        entry = state.start(scenario, _aligned_now())
        return JSONResponse(
            content={
                "scenario_id": scenario.id,
                "fingerprint": scenario.fingerprint(),
                "t0": entry.t0.isoformat(),
                "window": {
                    "start": entry.window_start().isoformat(),
                    "end": entry.window_end().isoformat(),
                },
            }
        )

    @app.post("/v1/scenarios/stop")
    def stop_scenarios() -> dict:
        return {"stopped": state.stop_all()}

    @app.get("/v1/metrics")
    def get_metrics(
        service: str,
        metric: str,
        window_minutes: int | None = Query(default=None, ge=1, le=1440),
        offset_minutes: int | None = Query(default=None, ge=-1440, le=1440),
    ) -> JSONResponse:
        try:
            active = resolve_service(service)
        except KeyError:
            return _error(404, "unknown_service", f"no scenario declares service {service}")
        if metric not in active.scenario.services[service].metrics:
            return _error(
                404,
                "unknown_metric",
                f"service {service} has no metric {metric} in scenario {active.scenario.id}",
            )
        points, coverage = generator.metric_series(
            active, service, metric, window_minutes, offset_minutes
        )
        return JSONResponse(
            content={
                "service": service,
                "metric": metric,
                "unit": active.scenario.services[service].metrics[metric].unit,
                "scenario_id": None if active.baseline_only else active.scenario.id,
                "coverage": coverage,
                "points": points,
            }
        )

    @app.get("/v1/logs")
    def get_logs(
        service: str,
        query: str | None = None,
        window_minutes: int | None = Query(default=None, ge=1, le=1440),
        offset_minutes: int | None = Query(default=None, ge=-1440, le=1440),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> JSONResponse:
        try:
            active = resolve_service(service)
        except KeyError:
            return _error(404, "unknown_service", f"no scenario declares service {service}")
        entries, coverage = generator.log_entries(
            active, service, window_minutes, offset_minutes, query, limit
        )
        return JSONResponse(
            content={
                "service": service,
                "scenario_id": None if active.baseline_only else active.scenario.id,
                "coverage": coverage,
                "entries": entries,
            }
        )

    @app.get("/v1/deployments")
    def get_deployments(service: str) -> JSONResponse:
        try:
            active = resolve_service(service)
        except KeyError:
            return _error(404, "unknown_service", f"no scenario declares service {service}")
        return JSONResponse(
            content={
                "service": service,
                "scenario_id": None if active.baseline_only else active.scenario.id,
                "deployments": generator.deployments(active, service),
            }
        )

    @app.get("/v1/queues")
    def get_queues(
        queue: str,
        window_minutes: int | None = Query(default=None, ge=1, le=1440),
        offset_minutes: int | None = Query(default=None, ge=-1440, le=1440),
    ) -> JSONResponse:
        try:
            active = resolve_queue(queue)
        except KeyError:
            return _error(404, "unknown_queue", f"no scenario declares queue {queue}")
        points, coverage, consumers = generator.queue_series(
            active, queue, window_minutes, offset_minutes
        )
        return JSONResponse(
            content={
                "queue": queue,
                "consumer_count": consumers,
                "scenario_id": None if active.baseline_only else active.scenario.id,
                "coverage": coverage,
                "points": points,
            }
        )

    @app.get("/v1/runtime-events")
    def get_runtime_events(
        service: str,
        window_minutes: int | None = Query(default=None, ge=1, le=1440),
        offset_minutes: int | None = Query(default=None, ge=-1440, le=1440),
    ) -> JSONResponse:
        try:
            active = resolve_service(service)
        except KeyError:
            return _error(404, "unknown_service", f"no scenario declares service {service}")
        events, coverage = generator.runtime_events(
            active, service, window_minutes, offset_minutes
        )
        return JSONResponse(
            content={
                "service": service,
                "scenario_id": None if active.baseline_only else active.scenario.id,
                "coverage": coverage,
                "events": events,
            }
        )

    # -- 动作端点 ---------------------------------------------------------
    #
    # 动作只作用于内存中的合成状态（NG-2、NG-3）：不重启任何真实进程，不触碰任何
    # 真实资源。它们存在的意义是让 Agent 在处置后能验证效果（E2E 主线 1 的最后一步）。
    #
    # 这些端点**不做授权判定**。审批的权威在 Java Control Plane，Policy 在 Agent
    # Runtime；synthetic-lab 只是被作用的对象。把授权放这里会多一个需要证明
    # 「审批没被绕过」的地方，而它不提供任何增量。

    @app.post("/v1/actions/restart")
    def action_restart(request: RestartRequest) -> JSONResponse:
        record = ledger.restart(
            idempotency_key=request.idempotency_key, service=request.service
        )
        return JSONResponse(content=_action_response(record))

    @app.post("/v1/actions/rollback")
    def action_rollback(request: RollbackRequest) -> JSONResponse:
        try:
            record = ledger.rollback(
                idempotency_key=request.idempotency_key,
                service=request.service,
                target_version=request.target_version,
            )
        except ActionRejected as exc:
            return _error(409, exc.code, str(exc))
        return JSONResponse(content=_action_response(record))

    @app.post("/v1/actions/throttle")
    def action_throttle(request: ThrottleRequest) -> JSONResponse:
        try:
            record = ledger.throttle(
                idempotency_key=request.idempotency_key,
                service=request.service,
                rate_basis_points=request.rate_basis_points,
            )
        except ActionRejected as exc:
            return _error(422, exc.code, str(exc))
        return JSONResponse(content=_action_response(record))

    @app.get("/v1/actions")
    def list_actions(service: str | None = None) -> dict:
        return {
            "actions": [_action_response(r) for r in ledger.history(service)],
        }

    @app.get("/v1/service-state")
    def service_state(service: str) -> dict:
        state = ledger.state_of(service)
        return {
            "service": state.service,
            "deployed_version": state.deployed_version,
            "restart_count": state.restart_count,
            "traffic_basis_points": state.traffic_basis_points,
            "last_action": state.last_action,
        }

    @app.post("/v1/actions/reset")
    def reset_actions() -> dict:
        ledger.reset()
        return {"reset": True}

    @app.get("/v1/scenarios/{scenario_id}/snapshot")
    def snapshot(scenario_id: str) -> JSONResponse:
        """剧本的全量数据快照 + 关键判据。

        存在的理由：稳定性校验需要一次拿到该剧本的全部信号，逐个端点查询会
        因查询顺序不同而难以比对。判据字段（alignment）由本服务计算而非
        由测试重算，使「S3 的错误率跃升与部署对齐」这类断言有唯一口径。
        """
        try:
            library.get(scenario_id)
        except KeyError:
            return _error(404, "scenario_not_found", f"unknown scenario {scenario_id}")
        active = state.active.get(scenario_id)
        if active is None:
            return _error(409, "scenario_not_active", f"scenario {scenario_id} is not running")

        scenario = active.scenario
        services_payload = {}
        for name, svc in sorted(scenario.services.items()):
            metrics = {}
            for metric in sorted(svc.metrics):
                points, _ = generator.metric_series(active, name, metric, None, None)
                metrics[metric] = points
            logs, _ = generator.log_entries(active, name, None, None, None, 2000)
            events, _ = generator.runtime_events(active, name, None, None)
            services_payload[name] = {
                "metrics": metrics,
                "logs": logs,
                "deployments": generator.deployments(active, name),
                "runtime_events": events,
            }

        queues_payload = {}
        for qname in sorted(scenario.queues):
            points, _, consumers = generator.queue_series(active, qname, None, None)
            queues_payload[qname] = {"consumer_count": consumers, "points": points}

        return JSONResponse(
            content={
                "scenario_id": scenario.id,
                "fingerprint": scenario.fingerprint(),
                "t0": active.t0.isoformat(),
                "services": services_payload,
                "queues": queues_payload,
                "alignment": _alignment(active),
            }
        )

    return app


def _alignment(active: ActiveScenario) -> dict:
    """关键判据。放在服务端而非测试里，使断言有唯一口径。"""
    scenario = active.scenario
    result: dict[str, bool] = {}

    for name, svc in scenario.services.items():
        if "http_error_rate" in svc.metrics and svc.deployments:
            spec = svc.metrics["http_error_rate"]
            non_decoy = [d for d in svc.deployments if not d.is_decoy]
            # step 形态意味着跃升发生在 T0；部署 at_minute == 0 即与之对齐。
            result["error_rate_jump_matches_deployment"] = bool(
                spec.shape == "step"
                and any(d.at_minute == 0 for d in non_decoy)
                and spec.incident > spec.baseline
            )

        oom = [e for e in svc.runtime_events if e.event_type == "OOMKilled"]
        if oom and "memory_used_bytes" in svc.metrics:
            mem = svc.metrics["memory_used_bytes"]
            limit = svc.metrics.get("memory_limit_bytes")
            touches_limit = bool(limit and mem.incident >= limit.baseline)
            events, _ = generator.runtime_events(active, name, None, None)
            in_window = len(events) > 0
            result["oom_events_align_with_memory_peaks"] = bool(
                touches_limit and in_window and mem.shape == "sawtooth"
            )

    return result
