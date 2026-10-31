"""The single local writer for grid-charge grants.

One arbitration point for every device write (`_write_lock`), one cycle at a
time (`_cycle_lock`). A safety stop does not wait for the cycle: it bumps
`safety_revision` first — an enabling sequence re-checks it before each of
its writes and aborts — then writes `grid_charge` off and every slot
`charge_enable` off, before anything is persisted.

Writes happen only when the gates are explicitly live (shadow switched off
AND the feature switched on, both readable). Otherwise every intended write
is only reported, nothing is written, and an enabling proposal is not
persisted (no pending step that nobody will execute).
A series of floor writes re-checks the safety revision, the gates, the runtime, grant /
pending step, a fresh charging readback and the trust in the inputs before EACH lowering
(raises are always safe and are not held back). The only exception is
`async_release`: leaving live mode turns charging off once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, time

from homeassistant.core import HomeAssistant

from .device_io import DeviceIO, WriteResult
from .energy_core.grant import DEFAULT_GRANT_BUDGET_FACTOR, CounterReading
from .floor_table import FloorPlan
from .lifecycle import confirm_closed, record_step_result, step_grant
from .recovery_flow import floor_writes
from .runtime import EnergyRuntime
from .slot_plan import (
    CAPACITY_TOLERANCE_PCT,
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
from .start_gate import StartGate
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
    hold_reason: str | None = None  # something outside forbids grid charging for now


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
        """Grid-charge grants: the shadow switch reads `off` AND the feature switch `on`."""
        return self._state(self.shadow_entity) == "off" and self._state(self.feature_entity) == "on"

    def floors_live(self) -> bool:
        """Slot floors (when floor management is configured): only the shadow switch matters,
        so the floors can be taken over without ever allowing grid charging."""
        return self._state(self.shadow_entity) == "off"


@dataclass
class CycleReport:
    charge_enable: bool = False
    reason: str | None = None
    live: bool = False
    floors_live: bool = False
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
        lowering_recheck: Callable[[], bool] | None = None,
        budget_factor: float = DEFAULT_GRANT_BUDGET_FACTOR,
        start_gate: StartGate | None = None,
    ) -> None:
        self.start_gate = start_gate or StartGate()
        self.runtime = runtime
        self.io = io
        self.entities = entities
        self.gates = gates
        self.recheck = recheck or (lambda: True)
        # fresh look at the inputs a floor lowering rests on (telemetry, tariff schedule);
        # without it nothing beyond the executor's own state is re-checked
        self.lowering_recheck = lowering_recheck or (lambda: True)
        self.budget_factor = budget_factor
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

    async def async_cycle(self, snapshot: Snapshot, *, floor_plan: FloorPlan | None = None,
                          lowering_ok: bool = False) -> CycleReport:
        """`floor_plan` = the table floors to keep (only when floor management is
        on); `lowering_ok` = the plan was built from trustworthy inputs, so a floor
        may also be LOWERED (raising is always allowed)."""
        async with self._cycle_lock:
            start_free = self.start_gate.try_acquire()
            try:
                return await self._cycle_locked(
                    snapshot, floor_plan=floor_plan, lowering_ok=lowering_ok,
                    start_free=start_free)
            finally:
                if start_free:
                    self.start_gate.release()

    async def _cycle_locked(self, snapshot: Snapshot, *, floor_plan: FloorPlan | None,
                            lowering_ok: bool, start_free: bool) -> CycleReport:
        """The body of a cycle. `start_free`: the start gate was ours; when the car's writer
        is starting something, nothing here may enable grid charging this time."""
        report = CycleReport(live=self.gates.live(), floors_live=self.gates.floors_live())
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
            budget_factor=self.budget_factor,
            hold_reason=snapshot.hold_reason or (None if start_free else "ev_starting"),
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
        if floor_plan is None:
            await self._restore_floors(report)
        else:
            await self._apply_floor_plan(floor_plan, lowering_ok, report)
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

    async def _run_floors(self, ops: list[Op], rev: int, lowering: frozenset[str],
                          report: CycleReport, gate: Callable[[], bool]) -> list[WriteResult]:
        """Floor writes in sequence. A RAISE is always safe and goes ahead; before EACH
        LOWERING the conditions it was planned under are looked at again, under the write
        lock, so a safety stop, closed gates, a grant, charging or inputs that stopped
        being trustworthy between two writes end the series right there."""
        results: list[WriteResult] = []
        for op in ops:
            async with self._write_lock:
                if op.entity_id in lowering:
                    reason = self._lowering_blocker(rev, gate)
                    if reason is not None:
                        report.error = f"floor lowering stopped: {reason}"
                        break
                result = await self.io.write(op)
            results.append(result)
            if result.outcome != "confirmed":
                break
        return results

    def _grant_owns_floors(self) -> bool:
        """A grant that is real owns the floors; one that is only PREPARED while grants are not
        live (shadow) changes nothing physical and leaves the floors to the table."""
        active = None if self.runtime.state is None else self.runtime.state.active_grant
        return active is not None and (active.status != "prepared" or self.gates.live())

    def _lowering_blocker(self, rev: int, gate: Callable[[], bool]) -> str | None:
        """Why the next floor LOWERING must not be written; `None` = it may."""
        state = self.runtime.state
        if self.safety_revision != rev:
            return "safety_stop"
        if not gate():
            return "not_live"
        if state is None or not self.runtime.may_enable:
            return "runtime_not_ready"
        if self._grant_owns_floors() or state.pending_step is not None:
            return "grant_or_step_in_play"
        if self.io.read_slots(self.entities).charging_enabled is not False:
            return "charging_not_verified_off"
        if not self.lowering_recheck():
            return "inputs_not_trusted"
        return None

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

    async def _apply_floor_plan(self, plan: FloorPlan, lowering_ok: bool,
                                report: CycleReport) -> None:
        """Keep the slot floors at the table plan. Never while a grant, a pending step
        or charging is in play (the grant owns the floors then); raises first, lowers
        only with `lowering_ok`; a plan that fails its sanity check is never written;
        stops at the first write that is not confirmed by readback."""
        state = self.runtime.state
        if (
            state is None
            or self._grant_owns_floors()
            or state.pending_step is not None
            or not self.runtime.may_enable
        ):
            return
        error = plan.sanity_error()
        if error is not None:
            report.error = f"floor plan refused: {error}"
            return
        readback = self.io.read_slots(self.entities)
        if readback.charging_enabled is not False:
            return
        raises, lowers = [], []
        for entity, planned, current in zip(self.entities.slot_capacity, plan.floors,
                                            readback.slot_capacity, strict=True):
            op = Op(entity, float(planned))
            if current is None:
                raises.append(op)  # unknown: write the safe (planned) value
            elif planned > current + CAPACITY_TOLERANCE_PCT:
                raises.append(op)
            elif planned < current - CAPACITY_TOLERANCE_PCT:
                lowers.append(op)
        ops = raises + (lowers if lowering_ok else [])
        if not ops:
            return
        if report.floors_live:
            report.writes += await self._run_floors(
                ops, self.safety_revision,
                frozenset(op.entity_id for op in lowers), report, self.gates.floors_live)
        else:
            report.intents += ops

    async def _restore_floors(self, report: CycleReport) -> None:
        """After a recovery grant: keep the hold floor (`cloudy_hold`, raising
        back a floor we had already restored) or write the restore floors
        (`restoring`). Only with charging verifiably off and no grant, never
        while enabling; a floor changed from outside blocks every write."""
        state = self.runtime.state
        recovery = None if state is None else state.recovery
        if (
            state is None
            or state.active_grant is not None
            or recovery is None
            or recovery.state not in ("cloudy_hold", "restoring", "wait_pv")
        ):
            return
        readback = self.io.read_slots(self.entities)
        if readback.charging_enabled is not False:
            return
        planned = floor_writes(recovery, readback.slot_capacity)
        if not planned:
            if planned is None and report.live:
                report.error = "floor_conflict: floors changed from outside, nothing written"
            return
        ops = [Op(self.entities.slot_capacity[i], float(value)) for i, value in planned]
        lowering = frozenset(
            self.entities.slot_capacity[i] for i, value in planned
            if readback.slot_capacity[i] is not None
            and value < readback.slot_capacity[i] - CAPACITY_TOLERANCE_PCT)
        if report.live:
            report.writes += await self._run_floors(
                ops, self.safety_revision, lowering, report, self.gates.live)
        else:
            report.intents += ops

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

