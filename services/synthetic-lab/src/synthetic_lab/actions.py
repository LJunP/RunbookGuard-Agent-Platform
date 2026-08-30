"""动作工具的作用对象。

M0 §8 与 NG-2：所有处置只作用于合成环境。这里的「重启」不重启任何真实进程，
它只是把一条可查询的动作记录与状态变更写进内存——足以让 Agent 在动作后
验证效果（E2E 主线 1 的最后一步），且不可能影响任何真实系统。

幂等由 idempotency_key 保证：同一个键重复提交返回首次结果，
副作用只发生一次（ADR-0003 的推论）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


class ActionRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ActionRecord:
    idempotency_key: str
    action: str
    service: str
    detail: dict
    performed_at: str
    # 第二次提交同一个键时返回的是这条记录，且 replayed 标记为真——
    # 让调用方能区分「刚做的」与「之前做过的」。
    replayed: bool = False


@dataclass
class ServiceRuntimeState:
    """动作作用后的可观测状态。Agent 在处置后查它来验证效果。"""

    service: str
    deployed_version: str
    restart_count: int = 0
    traffic_basis_points: int = 10000
    last_action: str | None = None


class ActionLedger:
    """动作账本。进程内内存，随 synthetic-lab 重启清空——它是测试替身，不是业务事实。"""

    def __init__(self) -> None:
        self._records: dict[str, ActionRecord] = {}
        self._state: dict[str, ServiceRuntimeState] = {}

    def state_of(self, service: str) -> ServiceRuntimeState:
        if service not in self._state:
            self._state[service] = ServiceRuntimeState(
                service=service, deployed_version="v1.5.0"
            )
        return self._state[service]

    def _now(self) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat()

    def _replay_or_new(self, key: str) -> ActionRecord | None:
        existing = self._records.get(key)
        if existing is None:
            return None
        return ActionRecord(
            idempotency_key=existing.idempotency_key,
            action=existing.action,
            service=existing.service,
            detail=dict(existing.detail),
            performed_at=existing.performed_at,
            replayed=True,
        )

    def restart(self, *, idempotency_key: str, service: str) -> ActionRecord:
        replayed = self._replay_or_new(idempotency_key)
        if replayed is not None:
            return replayed
        state = self.state_of(service)
        state.restart_count += 1
        state.last_action = "restart"
        record = ActionRecord(
            idempotency_key=idempotency_key,
            action="restart",
            service=service,
            detail={"restart_count": state.restart_count},
            performed_at=self._now(),
        )
        self._records[idempotency_key] = record
        return record

    def rollback(self, *, idempotency_key: str, service: str, target_version: str) -> ActionRecord:
        replayed = self._replay_or_new(idempotency_key)
        if replayed is not None:
            return replayed
        state = self.state_of(service)
        if target_version == state.deployed_version:
            # 回滚到当前版本是无操作。拒绝而不是静默成功——
            # 静默成功会让「回滚生效了吗」这个验证步骤失去意义。
            raise ActionRejected(
                "target_version_already_deployed",
                f"{service} is already on {target_version}",
            )
        previous = state.deployed_version
        state.deployed_version = target_version
        state.last_action = "rollback"
        record = ActionRecord(
            idempotency_key=idempotency_key,
            action="rollback",
            service=service,
            detail={"from": previous, "to": target_version},
            performed_at=self._now(),
        )
        self._records[idempotency_key] = record
        return record

    def throttle(
        self, *, idempotency_key: str, service: str, rate_basis_points: int
    ) -> ActionRecord:
        replayed = self._replay_or_new(idempotency_key)
        if replayed is not None:
            return replayed
        if not 0 <= rate_basis_points <= 10000:
            raise ActionRejected(
                "rate_out_of_range", "rate_basis_points must be within [0, 10000]"
            )
        state = self.state_of(service)
        state.traffic_basis_points = rate_basis_points
        state.last_action = "throttle"
        record = ActionRecord(
            idempotency_key=idempotency_key,
            action="throttle",
            service=service,
            detail={"rate_basis_points": rate_basis_points},
            performed_at=self._now(),
        )
        self._records[idempotency_key] = record
        return record

    def history(self, service: str | None = None) -> list[ActionRecord]:
        records = sorted(self._records.values(), key=lambda r: r.performed_at)
        if service:
            records = [r for r in records if r.service == service]
        return records

    def reset(self) -> None:
        self._records.clear()
        self._state.clear()
