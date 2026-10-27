"""The single local writer for grid-charge grants.

One arbitration point for every device write (`_write_lock`), one cycle at a
time (`_cycle_lock`). A safety stop does not wait for the cycle: it bumps
`safety_revision` first — an enabling sequence re-checks it before each of
its writes and aborts — then writes `grid_charge` off and every slot
`charge_enable` off, before anything is persisted.

Writes happen only when the gates are explicitly live (shadow switched off
AND the feature switched on, both readable). Otherwise every intended write
is only reported, nothing is written, and an enabling proposal is not
persisted (no pending step that nobody will execute). The only exception is
`async_release`: leaving live mode turns charging off once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, time

from homeassistant.core import HomeAssistant

from .device_io import DeviceIO, WriteResult
from .energy_core.grant import CounterReading
from .lifecycle import confirm_closed, record_step_result, step_grant
from .runtime import EnergyRuntime
from .slot_plan import (
    Op,
    Readback,
    SlotEntities,
    current_slot,
    enable_ops,
    floors_at_least,
    is_prepared,
    prepare_ops,
    stop_ops,
)
from .state_schema import StoreState
from .store import StoreNotDurableError


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    local_time: time
    soc_pct: float | None
    telemetry_valid: bool
    reading: CounterReading
    reading_epoch: str
    is_nt: Callable[[datetime], bool]
    slot_starts: tuple[time, ...] | None = None  # read from the inverter; None = defaults


class Gates:
    """Live only when the shadow switch reads `off` AND the feature switch
    reads `on`. Missing, unavailable or unconfigured = not live."""

    def __init__(self, hass: HomeAssistant, *, shadow_entity: str | None,
                 feature_entity: str | None) -> None:
        self.hass = hass
        self.shadow_entity = shadow_entity
        self.feature_entity = feature_entity

    def _state(self, entity_id: str | None) -> str | None:
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        return None if state is None else state.state

    def live(self) -> bool:
        return self._state(self.shadow_entity) == "off" and self._state(self.feature_entity) == "on"


@dataclass
class CycleReport:
    charge_enable: bool = False
    reason: str | None = None
    live: bool = False
    intents: list[Op] = field(default_factory=list)
    writes: list[WriteResult] = field(default_factory=list)
    step_outcome: str | None = None
    error: str | None = None


class GrantExecutor:
    def __init__(
        self,
        runtime: EnergyRuntime,
        io: DeviceIO,
        entities: SlotEntities,
        gates: Gates,
        *,
        recheck: Callable[[], bool] | None = None,
    ) -> None:
        self.runtime = runtime
        self.io = io
        self.entities = entities
        self.gates = gates
        self.recheck = recheck or (lambda: True)
        self.safety_revision = 0
        self._cycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    # --- safety stop -----------------------------------------------------------------

    async def async_safety_stop(self) -> list[WriteResult]:
        """Preempts any enabling sequence; never waits for the Store."""
        self.safety_revision += 1
        if not self.gates.live():
            return []
        return await self._stop()

    async def async_release(self) -> list[WriteResult]:
        """Stop when leaving live mode (gates just closed, unload): the one
        write that ignores the gates — it only ever turns charging off."""
        self.safety_revision += 1
        return await self._stop()

    async def _stop(self) -> list[WriteResult]:
        results = []
        for op in stop_ops(self.entities):  # every op attempted even if one fails
            async with self._write_lock:
                results.append(await self.io.write(op))
        return results

    # --- one cycle -------------------------------------------------------------------

    async def async_cycle(self, snapshot: Snapshot) -> CycleReport:
        async with self._cycle_lock:
            report = CycleReport(live=self.gates.live())
            state = self.runtime.state
            if state is None or self.runtime.blocked_reason is not None:
                report.error = f"runtime blocked: {self.runtime.blocked_reason}"
                return report
            rev = self.safety_revision
            readback = self.io.read_slots(self.entities)
            active = state.active_grant

            if active is not None and active.status in ("prepared", "paused"):
                target = active.identity.target_pct
                if not is_prepared(target, readback):
                    ops = prepare_ops(self.entities, target, readback)
                    if report.live and self.runtime.may_enable:
                        report.writes += await self._run(ops, rev, enabling=False)
                        readback = self.io.read_slots(self.entities)
                    else:
                        report.intents += ops

            result = step_grant(
                state,
                now=snapshot.now,
                soc_now_pct=snapshot.soc_pct if snapshot.soc_pct is not None else 0.0,
                reading=snapshot.reading,
                reading_epoch=snapshot.reading_epoch,
                telemetry_valid=snapshot.telemetry_valid and snapshot.soc_pct is not None,
                is_nt=snapshot.is_nt,
                deye_prepared=_prepared_for(state, readback),
                safety_revision=rev,
                observed_before={"grid_charge": readback.grid_charge},
            )
            report.charge_enable, report.reason = result.charge_enable, result.reason
            new_state, step_id = result.state, result.step_id
            if step_id is not None and not (report.live and self.runtime.may_enable):
                report.intents += self._enable_ops(new_state, snapshot)
                new_state = replace(new_state, pending_step=state.pending_step)
                step_id = None

            # Stopping first, persisting second: a stop never waits for the disk.
            if not result.charge_enable and readback.charging_enabled is not False:
                if report.live:
                    report.writes += await self.async_safety_stop()
                    readback = self.io.read_slots(self.entities)
                else:
                    report.intents += stop_ops(self.entities)

            if not await self._commit(new_state, report):
                return report
            if step_id is not None:
                await self._enable(step_id, rev, snapshot, report)
            await self._close_if_stopped(snapshot.now, report)
            return report

    # --- internals ---------------------------------------------------------------------

    def _enable_ops(self, state: StoreState, snapshot: Snapshot) -> list[Op]:
        active = state.active_grant
        assert active is not None
        entities = self.entities
        if snapshot.slot_starts is not None:
            entities = replace(entities, slot_starts=snapshot.slot_starts)
        slot = current_slot(entities, snapshot.local_time)
        return enable_ops(entities, slot, active.identity.target_pct)

    async def _enable(self, step_id: str, rev: int, snapshot: Snapshot,
                      report: CycleReport) -> None:
        state = self.runtime.state
        assert state is not None and state.active_grant is not None
        sent = record_step_result(
            state, step_id=step_id, operation="charge_enable", outcome="sent", now=snapshot.now
        )
        if not await self._commit(sent, report):
            return
        ops = self._enable_ops(sent, snapshot)
        writes = await self._run(ops, rev, enabling=True,
                                 grant_id=state.active_grant.identity.grant_id)
        report.writes += writes
        if len(writes) == len(ops) and all(w.outcome == "confirmed" for w in writes):
            outcome = "confirmed"
        else:
            # aborted or partial: never leave a half-enabled inverter behind
            await self.async_safety_stop()
            after = self.io.read_slots(self.entities).charging_enabled
            outcome = "failed" if after is False else "unknown"
        report.step_outcome = outcome
        await self._commit(
            record_step_result(self.runtime.state, step_id=step_id, operation="charge_enable",
                               outcome=outcome, now=snapshot.now),
            report,
        )

    async def _run(self, ops: list[Op], rev: int, *, enabling: bool,
                   grant_id: str | None = None) -> list[WriteResult]:
        results: list[WriteResult] = []
        for op in ops:
            async with self._write_lock:
                if enabling and not self._may_continue(rev, grant_id):
                    break
                result = await self.io.write(op)
            results.append(result)
            if result.outcome != "confirmed":
                break
        return results

    def _may_continue(self, rev: int, grant_id: str | None) -> bool:
        state = self.runtime.state
        active = None if state is None else state.active_grant
        return (
            self.safety_revision == rev
            and self.gates.live()
            and self.runtime.may_enable
            and active is not None
            and active.identity.grant_id == grant_id
            and active.status != "closing"
            and self.recheck()
        )

    async def _commit(self, new_state: StoreState, report: CycleReport) -> bool:
        if new_state == self.runtime.state:
            return True
        try:
            await self.runtime.async_commit(new_state)
        except StoreNotDurableError as exc:
            report.error = f"state not persisted: {exc}"
            if report.live:
                await self.async_safety_stop()
            return False
        return True

    async def _close_if_stopped(self, now: datetime, report: CycleReport) -> None:
        state = self.runtime.state
        if (
            state is not None
            and state.active_grant is not None
            and state.active_grant.status == "closing"
            and state.pending_step is None
            and self.io.read_slots(self.entities).charging_enabled is False
        ):
            await self._commit(confirm_closed(state, now=now), report)


def _prepared_for(state: StoreState, readback: Readback) -> bool:
    active = state.active_grant
    if active is None:
        return False
    target = active.identity.target_pct
    if active.status == "active":
        return floors_at_least(target, readback.slot_capacity)
    return is_prepared(target, readback)

