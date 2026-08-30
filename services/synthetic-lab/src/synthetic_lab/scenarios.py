"""剧本模型与加载。

剧本是数据不是代码（ADR-0004 §1）：必须能被评测集引用、版本化、diff、贴进 Gate 报告。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .shapes import SHAPES


class ScenarioError(Exception):
    """剧本定义本身有问题。启动时抛出，不静默降级。"""


@dataclass(frozen=True)
class MetricSpec:
    baseline: float
    incident: float
    shape: str
    unit: str = ""
    # 配置常量与饱和值必须精确：连接池上限、内存 limit 不会抖动，
    # 饱和的连接池就是恰好等于上限。给它们加噪声会产出物理上不可能的数据。
    noise: float = 0.03

    def validate(self, ctx: str) -> None:
        if self.shape not in SHAPES:
            raise ScenarioError(f"{ctx}: unknown shape {self.shape!r}")
        if not 0.0 <= self.noise <= 0.5:
            raise ScenarioError(f"{ctx}: noise must be within [0, 0.5], got {self.noise}")


@dataclass(frozen=True)
class LogTemplate:
    level: str
    message: str
    phase: str = "incident"  # baseline | incident | both
    per_minute: int = 2


@dataclass(frozen=True)
class InjectedLine:
    """注入载荷。原样写入日志流，不做任何过滤（ADR-0004 §7）。"""

    message: str
    level: str = "WARN"
    at_minute: int = 1


@dataclass(frozen=True)
class DeploymentSpec:
    version: str
    previous_version: str
    at_minute: int  # 相对 T0 的偏移，负数表示 T0 之前
    changed_config_keys: list[str] = field(default_factory=list)
    is_decoy: bool = False


@dataclass(frozen=True)
class QueueSpec:
    name: str
    depth: MetricSpec
    oldest_age_seconds: MetricSpec
    publish_rate: MetricSpec
    deliver_rate: MetricSpec
    consumer_count: int = 3


@dataclass(frozen=True)
class RuntimeEventSpec:
    event_type: str
    exit_code: int | None = None
    reason: str = ""
    count: int = 1
    first_at_minute: int = 1
    interval_minutes: int = 1


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    metrics: dict[str, MetricSpec] = field(default_factory=dict)
    logs: list[LogTemplate] = field(default_factory=list)
    injected_lines: list[InjectedLine] = field(default_factory=list)
    deployments: list[DeploymentSpec] = field(default_factory=list)
    runtime_events: list[RuntimeEventSpec] = field(default_factory=list)


@dataclass(frozen=True)
class Scenario:
    id: str
    version: int
    description: str
    seed: int
    baseline_minutes: int
    incident_minutes: int
    services: dict[str, ServiceSpec]
    queues: dict[str, QueueSpec] = field(default_factory=dict)
    sample_interval_seconds: int = 30

    def validate(self) -> None:
        if self.baseline_minutes <= 0 or self.incident_minutes <= 0:
            raise ScenarioError(f"{self.id}: timeline windows must be positive")
        for svc in self.services.values():
            for name, spec in svc.metrics.items():
                spec.validate(f"{self.id}/{svc.name}/{name}")
        for q in self.queues.values():
            for label, spec in (
                ("depth", q.depth),
                ("oldest_age_seconds", q.oldest_age_seconds),
                ("publish_rate", q.publish_rate),
                ("deliver_rate", q.deliver_rate),
            ):
                spec.validate(f"{self.id}/queue:{q.name}/{label}")

    def with_seed(self, seed: int) -> Scenario:
        return replace(self, seed=seed)

    def fingerprint(self) -> str:
        """剧本定义的摘要。冻结评测时用它证明「用的是这一版剧本」。"""
        payload = {
            "id": self.id,
            "version": self.version,
            "seed": self.seed,
            "baseline_minutes": self.baseline_minutes,
            "incident_minutes": self.incident_minutes,
            "sample_interval_seconds": self.sample_interval_seconds,
            "services": {
                name: {
                    "metrics": {
                        m: [s.baseline, s.incident, s.shape, s.noise]
                        for m, s in sorted(svc.metrics.items())
                    },
                    "logs": [[t.level, t.message, t.phase, t.per_minute] for t in svc.logs],
                    "injected": [[i.level, i.message, i.at_minute] for i in svc.injected_lines],
                    "deployments": [
                        [d.version, d.previous_version, d.at_minute, sorted(d.changed_config_keys), d.is_decoy]
                        for d in svc.deployments
                    ],
                    "runtime_events": [
                        [e.event_type, e.exit_code, e.reason, e.count, e.first_at_minute, e.interval_minutes]
                        for e in svc.runtime_events
                    ],
                }
                for name, svc in sorted(self.services.items())
            },
            "queues": {
                name: {
                    "consumer_count": q.consumer_count,
                    "depth": [q.depth.baseline, q.depth.incident, q.depth.shape],
                    "age": [q.oldest_age_seconds.baseline, q.oldest_age_seconds.incident, q.oldest_age_seconds.shape],
                    "publish": [q.publish_rate.baseline, q.publish_rate.incident, q.publish_rate.shape],
                    "deliver": [q.deliver_rate.baseline, q.deliver_rate.incident, q.deliver_rate.shape],
                }
                for name, q in sorted(self.queues.items())
            },
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def services_touched(self) -> set[str]:
        return set(self.services)


def _metric(raw: dict, ctx: str) -> MetricSpec:
    try:
        return MetricSpec(
            baseline=float(raw["baseline"]),
            incident=float(raw["incident"]),
            shape=str(raw["shape"]),
            unit=str(raw.get("unit", "")),
            noise=float(raw.get("noise", 0.03)),
        )
    except KeyError as missing:
        raise ScenarioError(f"{ctx}: metric missing field {missing}") from missing


def parse_scenario(raw: dict) -> Scenario:
    try:
        scenario_id = str(raw["id"])
        timeline = raw["timeline"]
    except KeyError as missing:
        raise ScenarioError(f"scenario missing field {missing}") from missing

    services: dict[str, ServiceSpec] = {}
    for svc_raw in raw.get("services", []):
        name = str(svc_raw["name"])
        services[name] = ServiceSpec(
            name=name,
            metrics={
                m: _metric(spec, f"{scenario_id}/{name}/{m}")
                for m, spec in (svc_raw.get("metrics") or {}).items()
            },
            logs=[
                LogTemplate(
                    level=str(t.get("level", "INFO")),
                    message=str(t["message"]),
                    phase=str(t.get("phase", "incident")),
                    per_minute=int(t.get("per_minute", 2)),
                )
                for t in (svc_raw.get("logs") or [])
            ],
            injected_lines=[
                InjectedLine(
                    message=str(i["message"]),
                    level=str(i.get("level", "WARN")),
                    at_minute=int(i.get("at_minute", 1)),
                )
                for i in (svc_raw.get("injected_lines") or [])
            ],
            deployments=[
                DeploymentSpec(
                    version=str(d["version"]),
                    previous_version=str(d["previous_version"]),
                    at_minute=int(d["at_minute"]),
                    changed_config_keys=[str(k) for k in (d.get("changed_config_keys") or [])],
                    is_decoy=bool(d.get("is_decoy", False)),
                )
                for d in (svc_raw.get("deployments") or [])
            ],
            runtime_events=[
                RuntimeEventSpec(
                    event_type=str(e["event_type"]),
                    exit_code=(int(e["exit_code"]) if e.get("exit_code") is not None else None),
                    reason=str(e.get("reason", "")),
                    count=int(e.get("count", 1)),
                    first_at_minute=int(e.get("first_at_minute", 1)),
                    interval_minutes=int(e.get("interval_minutes", 1)),
                )
                for e in (svc_raw.get("runtime_events") or [])
            ],
        )

    queues: dict[str, QueueSpec] = {}
    for q_raw in raw.get("queues", []):
        qname = str(q_raw["name"])
        queues[qname] = QueueSpec(
            name=qname,
            depth=_metric(q_raw["depth"], f"{scenario_id}/queue:{qname}/depth"),
            oldest_age_seconds=_metric(q_raw["oldest_age_seconds"], f"{scenario_id}/queue:{qname}/age"),
            publish_rate=_metric(q_raw["publish_rate"], f"{scenario_id}/queue:{qname}/publish"),
            deliver_rate=_metric(q_raw["deliver_rate"], f"{scenario_id}/queue:{qname}/deliver"),
            consumer_count=int(q_raw.get("consumer_count", 3)),
        )

    scenario = Scenario(
        id=scenario_id,
        version=int(raw.get("version", 1)),
        description=str(raw.get("description", "")),
        seed=int(raw.get("seed", 0)),
        baseline_minutes=int(timeline["baseline_minutes"]),
        incident_minutes=int(timeline["incident_minutes"]),
        sample_interval_seconds=int(raw.get("sample_interval_seconds", 30)),
        services=services,
        queues=queues,
    )
    scenario.validate()
    return scenario


class ScenarioLibrary:
    def __init__(self, scenarios: dict[str, Scenario]) -> None:
        self._scenarios = scenarios

    @classmethod
    def from_dir(cls, directory: Path) -> ScenarioLibrary:
        scenarios: dict[str, Scenario] = {}
        for path in sorted(directory.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            scenario = parse_scenario(raw)
            if scenario.id in scenarios:
                raise ScenarioError(f"duplicate scenario id {scenario.id} in {path}")
            scenarios[scenario.id] = scenario
        if not scenarios:
            raise ScenarioError(f"no scenarios found in {directory}")
        return cls(scenarios)

    @classmethod
    def from_builtin_dir(cls) -> ScenarioLibrary:
        return cls.from_dir(Path(__file__).parent / "scenarios_builtin")

    def get(self, scenario_id: str) -> Scenario:
        if scenario_id not in self._scenarios:
            raise KeyError(scenario_id)
        return self._scenarios[scenario_id]

    def all(self) -> list[Scenario]:
        return list(self._scenarios.values())

    def known_services(self) -> set[str]:
        found: set[str] = set()
        for scenario in self._scenarios.values():
            found |= scenario.services_touched()
        return found

    def known_queues(self) -> set[str]:
        found: set[str] = set()
        for scenario in self._scenarios.values():
            found |= set(scenario.queues)
        return found
