"""按查询窗口即时计算数据。

数据不是随真实时间累积的，而是按查询窗口算出来的（ADR-0004 §2）。
若靠后台线程累积，线程调度、GC、系统负载都会渗进数据，「连续 3 次一致」立刻失效；
而且评测 30~50 个 case 每个等 5 分钟不可接受。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, datetime

from .determinism import unit
from .scenarios import Scenario
from .shapes import value_at

OUTSIDE_WINDOW = "outside_scenario_window"
COVERED = "covered"


@dataclass(frozen=True)
class ActiveScenario:
    scenario: Scenario
    t0: datetime
    # 基线模式：剧本未启动。所有采样点强制走基线值，与「T0 尚未到来」等价，
    # 但不靠把 t0 挪到未来来实现——那样 offset_minutes 的语义会变得难以解释。
    baseline_only: bool = False

    def window_start(self) -> datetime:
        return self.t0 - timedelta(minutes=self.scenario.baseline_minutes)

    def window_end(self) -> datetime:
        if self.baseline_only:
            return self.t0
        return self.t0 + timedelta(minutes=self.scenario.incident_minutes)


def _sample_times(
    active: ActiveScenario,
    window_minutes: int | None,
    offset_minutes: int | None,
) -> tuple[list[datetime], str]:
    """返回采样时刻。

    两种查询模式：
      - 都不指定：返回剧本的完整覆盖窗口（基线段 + 故障段）。这是只读工具的默认用法。
      - 指定 offset_minutes：起点为 T0 + offset，跨度为 window_minutes。

    超出覆盖范围时返回空列表 + 明确的 coverage 标记，不插值——插值出来的数据
    没有对应的剧本声明，无法解释也无法作为证据。
    """
    scenario = active.scenario
    covered_start = active.window_start()
    covered_end = active.window_end()

    if offset_minutes is None:
        start = covered_start
        end = covered_end if window_minutes is None else min(
            covered_end, covered_start + timedelta(minutes=window_minutes)
        )
    else:
        span = window_minutes if window_minutes is not None else scenario.incident_minutes
        requested_start = active.t0 + timedelta(minutes=offset_minutes)
        requested_end = requested_start + timedelta(minutes=span)
        if requested_start >= covered_end or requested_end <= covered_start:
            return [], OUTSIDE_WINDOW
        start = max(requested_start, covered_start)
        end = min(requested_end, covered_end)

    step = timedelta(seconds=scenario.sample_interval_seconds)
    times: list[datetime] = []
    cursor = start
    # 闭区间：终点必须采样，否则 ramp 形态永远到不了 incident 值
    # （5 分钟窗口 / 30s 间隔时峰值只到 90%），S1 的「池使用率达到 1.0」就不成立。
    while cursor <= end:
        times.append(cursor)
        cursor += step
    if not times:
        return [], OUTSIDE_WINDOW
    return times, COVERED


def _progress(active: ActiveScenario, at: datetime) -> float:
    """T0 之前（或基线模式）返回 -1；故障期内返回 0..1。"""
    if active.baseline_only or at < active.t0:
        return -1.0
    span = active.scenario.incident_minutes * 60.0
    elapsed = (at - active.t0).total_seconds()
    return min(1.0, elapsed / span) if span > 0 else 1.0


def _bucket_index(active: ActiveScenario, at: datetime) -> int:
    """采样点的整数序号。作为伪随机坐标，保证同一时刻总是同一抖动。"""
    delta = (at - active.window_start()).total_seconds()
    return int(delta // active.scenario.sample_interval_seconds)


def metric_series(
    active: ActiveScenario,
    service: str,
    metric: str,
    window_minutes: int | None,
    offset_minutes: int | None,
) -> tuple[list[dict], str]:
    spec = active.scenario.services[service].metrics[metric]
    times, coverage = _sample_times(active, window_minutes, offset_minutes)
    points = [
        {
            "timestamp": t.isoformat(),
            "value": round(
                value_at(
                    spec.shape,
                    spec.baseline,
                    spec.incident,
                    _progress(active, t),
                    seed=active.scenario.seed,
                    coords=(service, metric, _bucket_index(active, t)),
                    noise_ratio=spec.noise,
                ),
                6,
            ),
        }
        for t in times
    ]
    return points, coverage


def log_entries(
    active: ActiveScenario,
    service: str,
    window_minutes: int | None,
    offset_minutes: int | None,
    query: str | None,
    limit: int,
) -> tuple[list[dict], str]:
    scenario = active.scenario
    svc = scenario.services[service]
    times, coverage = _sample_times(active, window_minutes, offset_minutes)
    if not times:
        return [], coverage

    # 日志的时间范围取到最后一个采样点所覆盖的区间末尾，而不是采样点本身。
    # 采样点是离散的（默认每 30s 一个），若用最后一个点做上界，落在它之后
    # 那半个采样间隔内的日志行会被静默丢弃——注入行恰好容易落在那里。
    start = times[0]
    end = times[-1] + timedelta(seconds=scenario.sample_interval_seconds)
    entries: list[dict] = []

    for template_index, template in enumerate(svc.logs):
        minute_cursor = start.replace(second=0, microsecond=0)
        while minute_cursor <= end:
            in_incident = (not active.baseline_only) and minute_cursor >= active.t0
            phase_ok = (
                template.phase == "both"
                or (template.phase == "incident" and in_incident)
                or (template.phase == "baseline" and not in_incident)
            )
            if phase_ok:
                minute_key = int((minute_cursor - active.window_start()).total_seconds() // 60)
                for n in range(template.per_minute):
                    # 行内偏移由 seed 决定，同一 (模板, 分钟, 序号) 永远落在同一秒。
                    second = int(unit(scenario.seed, service, template_index, minute_key, n) * 60)
                    at = minute_cursor + timedelta(seconds=second)
                    if at < start or at > end:
                        continue
                    entries.append(
                        {
                            "timestamp": at.isoformat(),
                            "level": template.level,
                            "service": service,
                            "message": template.message,
                            "injected": False,
                        }
                    )
            minute_cursor += timedelta(minutes=1)

    # 注入行：原样输出，不做任何过滤或转义（ADR-0004 §7）。
    if not active.baseline_only:
        for injected_index, injected in enumerate(svc.injected_lines):
            second = int(unit(scenario.seed, service, "injected", injected_index) * 60)
            at = active.t0 + timedelta(minutes=injected.at_minute, seconds=second)
            if start <= at <= end:
                entries.append(
                    {
                        "timestamp": at.isoformat(),
                        "level": injected.level,
                        "service": service,
                        "message": injected.message,
                        "injected": True,
                    }
                )

    entries.sort(key=lambda e: (e["timestamp"], e["message"]))
    if query:
        needle = query.lower()
        entries = [e for e in entries if needle in e["message"].lower()]
    return entries[:limit], coverage


def deployments(active: ActiveScenario, service: str) -> list[dict]:
    svc = active.scenario.services[service]
    return [
        {
            "service": service,
            "version": d.version,
            "previous_version": d.previous_version,
            "deployed_at": (active.t0 + timedelta(minutes=d.at_minute)).isoformat(),
            # 只有键名。配置值可能是凭据（威胁 T-3）。
            "changed_config_keys": list(d.changed_config_keys),
            "is_decoy": d.is_decoy,
        }
        for d in sorted(svc.deployments, key=lambda d: d.at_minute)
    ]


def queue_series(
    active: ActiveScenario,
    queue: str,
    window_minutes: int | None,
    offset_minutes: int | None,
) -> tuple[list[dict], str, int]:
    spec = active.scenario.queues[queue]
    times, coverage = _sample_times(active, window_minutes, offset_minutes)
    points = []
    for t in times:
        progress = _progress(active, t)
        bucket = _bucket_index(active, t)

        def val(metric_spec, label: str) -> float:
            return value_at(
                metric_spec.shape,
                metric_spec.baseline,
                metric_spec.incident,
                progress,
                seed=active.scenario.seed,
                coords=(queue, label, bucket),
                noise_ratio=metric_spec.noise,
            )

        points.append(
            {
                "timestamp": t.isoformat(),
                "depth": round(val(spec.depth, "depth")),
                "oldest_age_seconds": round(val(spec.oldest_age_seconds, "age"), 3),
                "publish_rate": round(val(spec.publish_rate, "publish"), 3),
                "deliver_rate": round(val(spec.deliver_rate, "deliver"), 3),
            }
        )
    return points, coverage, spec.consumer_count


def runtime_events(
    active: ActiveScenario,
    service: str,
    window_minutes: int | None,
    offset_minutes: int | None,
) -> tuple[list[dict], str]:
    svc = active.scenario.services[service]
    times, coverage = _sample_times(active, window_minutes, offset_minutes)
    if not times or active.baseline_only:
        return [], coverage
    start, end = times[0], times[-1]

    occurrences: list[tuple[datetime, object]] = []
    for spec in svc.runtime_events:
        for n in range(spec.count):
            at = active.t0 + timedelta(minutes=spec.first_at_minute + n * spec.interval_minutes)
            occurrences.append((at, spec))
    # 按时间排序后累计 restart_count：它是累计值，不是每次事件的独立属性。
    occurrences.sort(key=lambda pair: (pair[0], pair[1].event_type))

    events: list[dict] = []
    restart_counter = 0
    for at, spec in occurrences:
        if spec.event_type == "ContainerStarted":
            restart_counter += 1
        if not (start <= at <= end):
            continue
        events.append(
            {
                "service": service,
                "event_type": spec.event_type,
                "timestamp": at.isoformat(),
                "restart_count": restart_counter,
                "exit_code": spec.exit_code,
                "reason": spec.reason,
            }
        )
    return events, coverage
