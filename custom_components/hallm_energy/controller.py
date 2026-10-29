"""The periodic control loop: one tick at a time.

Each tick:

1. builds the input snapshot from Home Assistant states;
2. leaving live mode (gates just closed) turns charging off once;
3. after a persistence fault, retries a verified write first (reconcile is
   then required again) — nothing enables in between;
4. an `active` grant whose charging was switched off from outside is sent
   back through reconcile, which honours the intervention: the grant is closed
   (`external_stop`, audited) and not resumed;
5. while reconcile is not `ready`, reconciles against the read-only snapshot
   (stopping first if required, persisting second);
6. otherwise plans recovery (`recovery_flow`: episode, decision, grant —
   persisted before anything enables) and runs one executor cycle.

The safety stop service does not take the tick lock — it preempts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .audit_outbox import AuditOutbox
from .config import EnergyConfig
from .const import POLICY_VERSION
from .device_io import DeviceIO, WriteResult
from .executor import Gates, GrantExecutor, Snapshot
from .floor_table import FloorContext, FloorPlan, floor_plan, hold_floor_pct
from .inputs import (
    build_inputs,
    offpeak_steps,
    schedule_intervals,
    schedule_nt,
)
from .lifecycle import (
    GrantTransitionError,
    provision,
    reconcile,
    reprovision,
    set_manual_block,
    set_reconcile,
)
from .morning_flow import MorningInputs, MorningOutcome, plan_morning
from .notifier import Notifier
from .profile_receiver import ProfileReceiver, PvForecastReceiver
from .recovery_flow import RecoveryInputs, RecoveryOutcome, outlook_status, plan_recovery
from .runtime import EnergyRuntime
from .slot_plan import Op, Readback, current_slot, stop_ops
from .state_schema import StoreState
from .store import StoreNotDurableError

_LOGGER = logging.getLogger(__name__)
STEP = timedelta(minutes=15)


class ProvisionError(Exception):
    pass


@dataclass
class TickStatus:
    """Outcome of the last tick, for diagnostics only."""

    at: datetime | None = None
    live: bool = False
    result: str = "not_run"
    reason: str | None = None
    problems: tuple[str, ...] = ()
    intents: list[Op] = field(default_factory=list)
    writes: list[WriteResult] = field(default_factory=list)
    error: str | None = None
    recovery: RecoveryOutcome | None = None
    morning: MorningOutcome | None = None
    floors: dict | None = None  # planned vs actual slot floors (diagnostics only)


class EnergyController:
    def __init__(
        self,
        hass: HomeAssistant,
        runtime: EnergyRuntime,
        config: EnergyConfig,
        *,
        io: DeviceIO | None = None,
    ) -> None:
        self.hass = hass
        self.runtime = runtime
        self.config = config
        self.io = io or DeviceIO(hass)
        self.gates = Gates(hass, shadow_entity=config.shadow_entity,
                           feature_entity=config.feature_entity)
        self.executor = GrantExecutor(runtime, self.io, config.slot_entities, self.gates,
                                      lowering_recheck=self._lowering_inputs_trusted,
                                      budget_factor=config.budget_factor)
        self.status = TickStatus()
        self.profiles = ProfileReceiver()
        self.pv_forecasts = PvForecastReceiver()
        self.outbox = AuditOutbox(runtime, config.audit_topic, _mqtt_publisher(hass))
        self.notifier = Notifier(hass, config.notify_service)
        runtime.mode_provider = lambda: "live" if self.gates.live() else "shadow"
        self._lock = asyncio.Lock()
        self._outlook: dict | None = None
        self._was_live = False
        self._listeners: list[Callable[[], None]] = []

    # --- diagnostics listeners -----------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # --- loop ----------------------------------------------------------------------

    async def async_tick(self, now: datetime | None = None) -> TickStatus:
        async with self._lock:
            now = now or dt_util.utcnow()
            inputs = build_inputs(self.hass, self.config, now)
            status = TickStatus(at=now, live=self.gates.live(), problems=inputs.problems)
            try:
                if self._was_live and not status.live:
                    status.writes += await self.executor.async_release()
                self._was_live = status.live
                await self._step(inputs.snapshot, status)
            except Exception as exc:  # the loop must survive; nothing enables after it
                _LOGGER.exception("tick failed")
                status.result, status.error = "error", repr(exc)
            self.status = status
            self._notify()
        await self._observe_conditions(status)  # outside the control lock: never delays a cycle
        return status

    async def _observe_conditions(self, status: TickStatus) -> None:
        """Alerts for the owner (`notify_service`); edge-triggered with a repeat."""
        notifier = self.notifier
        if not notifier.enabled:
            return
        state, error = self.runtime.state, status.error or ""
        now = status.at
        checks = (
            ("floor_lowering_stopped", error.startswith("floor lowering stopped"),
             f"Floor writes were stopped before lowering a floor ({error.split(': ', 1)[-1]}); "
             "the remaining floors were left as they were."),
            ("floor_conflict", error.startswith("floor_conflict"),
             "Slot floors were changed from outside the integration; it writes nothing "
             "until they match again."),
            ("tick_error", status.result == "error",
             f"The control loop hit an error: {error}"),
            ("persistence", self.runtime.blocked_reason is not None
             or self.runtime.persistence_fault is not None
             or error.startswith("state not persisted"),
             "The local state could not be persisted; nothing is enabled until it is."),
            ("reconcile_blocked", state is not None and state.reconcile_state.value == "blocked",
             "The state does not match the device (reconcile blocked); "
             "grid charging stays off until it is resolved."),
            ("island", self._grid_present() is False,
             "The grid is absent (or the grid relay is unavailable): no grid charging, no "
             "morning exception."),
            ("audit_full", self.outbox.full,
             "The audit outbox is full: no new grid-charge grant can be recorded."),
            ("external_stop", state is not None and state.manual_block is not None
             and state.manual_block.reason == "external_stop",
             "Grid charging was switched off from outside while a grant was running. The grant "
             "was closed and grid charging is blocked until you turn off 'Block grid charging'."),
        )
        for kind, active, message in checks:
            await notifier.async_observe(kind, active, message, now)

    async def async_deliver_audit(self) -> None:
        """One audit delivery round (own worker; takes the control lock only briefly)."""
        await self.outbox.async_deliver(self._lock, dt_util.utcnow)
        self._notify()

    async def async_discard_audit(self) -> int:
        """Explicit removal of the audit backlog — serialized with the control loop."""
        async with self._lock:
            count = await self.outbox.async_discard()
        self._notify()
        return count

    async def async_set_manual_block(self, on: bool, reason: str = "user") -> None:
        """The block switch: no grid charging until it is lifted. Serialized with the loop;
        a grant in flight is closed by the next cycle through the normal stop path."""
        async with self._lock:
            state = self.runtime.state
            if state is None or self.runtime.blocked_reason is not None:
                return
            new = set_manual_block(state, on=on, reason=reason, now=dt_util.utcnow())
            if new != state:
                try:
                    await self.runtime.async_commit(new)
                except StoreNotDurableError as exc:
                    _LOGGER.warning("manual block not persisted: %s", exc)
        self._notify()

    async def async_handle_ack(self, payload: str | bytes) -> None:
        """Acknowledgement from the audit receiver — serialized with the control loop."""
        async with self._lock:
            await self.outbox.async_handle_ack(payload, dt_util.utcnow())
            self._notify()

    async def async_release(self) -> None:
        """Called on unload: if live, leave charging off behind."""
        async with self._lock:
            if self._was_live:
                await self.executor.async_release()
                self._was_live = False

    async def async_safety_stop(self) -> list[WriteResult]:
        results = await self.executor.async_safety_stop()
        await self.notifier.async_notify_once(
            "safety_stop", "A safety stop was requested: grid charging was switched off.")
        return results

    async def async_provision(self, *, again: bool = False) -> None:
        """Explicit: trust the configured charge counter from now on. `again` =
        the procedure out of a counter change / lost state (`reprovision`)."""
        async with self._lock:
            state = self.runtime.state
            if self.runtime.blocked_reason is not None or state is None:
                raise ProvisionError(f"runtime blocked: {self.runtime.blocked_reason}")
            if not again and state.provisioning_state != "uninitialized":
                raise ProvisionError(f"provisioning is {state.provisioning_state}")
            now = dt_util.utcnow()
            reading = build_inputs(self.hass, self.config, now).snapshot.reading
            if not reading.available:
                raise ProvisionError("charge counter is unavailable")
            transition = reprovision if again else provision
            try:
                new_state = transition(
                    state,
                    counter_id=reading.counter_id,
                    counter_epoch=self.config.counter_epoch,
                    trusted_raw_dc_kwh=reading.raw_kwh,
                    at=now,
                )
            except GrantTransitionError as exc:
                raise ProvisionError(str(exc)) from exc
            await self.runtime.async_commit(new_state)

    # --- one step ------------------------------------------------------------------

    async def _step(self, snapshot: Snapshot, status: TickStatus) -> None:
        runtime = self.runtime
        if runtime.blocked_reason is not None or runtime.state is None:
            status.result = f"blocked:{runtime.blocked_reason}"
            return
        readback = self.io.read_slots(self.config.slot_entities)
        if runtime.persistence_fault is not None and not await runtime.async_verify_storage():
            status.result = "persistence_fault"
            await self._stop_if_charging(readback, status)
            return

        state = runtime.state
        active = state.active_grant
        if (
            state.reconcile_state.value == "ready"
            and active is not None
            and active.status == "active"
            and state.pending_step is None
            and readback.charging_enabled is False
        ):
            if not await self._commit(set_reconcile(state, "required", "readback_off"), status):
                return

        if runtime.state.reconcile_state.value != "ready":
            await self._reconcile(snapshot, readback, status)
            if runtime.state.reconcile_state.value != "ready":
                return

        if not await self._plan_morning(snapshot, status):
            return
        if not await self._plan_recovery(snapshot, readback, status):
            return
        plan = self._report_floors(snapshot, readback, status)
        managed = self.config.manage_floors and plan is not None
        report = await self.executor.async_cycle(
            snapshot,
            floor_plan=plan if managed else None,
            lowering_ok=managed and snapshot.telemetry_valid and "unknown" not in plan.kinds,
        )
        status.result = "cycle"
        status.reason = report.reason
        status.intents += report.intents
        status.writes += report.writes
        status.error = report.error

    def _lowering_inputs_trusted(self) -> bool:
        """Looked at again before every floor lowering: the telemetry is valid right now and
        the tariff schedule is known (the conditions `lowering_ok` was granted under)."""
        now = dt_util.utcnow()
        snapshot = build_inputs(self.hass, self.config, now).snapshot
        return (snapshot.telemetry_valid and snapshot.soc_pct is not None
                and schedule_intervals(self.hass, self.config, now) is not None)

    async def _plan_morning(self, snapshot: Snapshot, status: TickStatus) -> bool:
        params = self.config.morning
        if params is None:
            return True
        inputs = MorningInputs(
            now=snapshot.now,
            tz=dt_util.get_default_time_zone(),
            soc_pct=snapshot.soc_pct if snapshot.telemetry_valid else None,
            profile=self.profiles.profile,
            pv=self.pv_forecasts.forecast,
            grid_present=self._grid_present() is True,
            in_recovery=self.runtime.state.recovery is not None,
        )
        state = self.runtime.state
        new_state, outcome = plan_morning(state, inputs, params)
        status.morning = outcome
        return new_state == state or await self._commit(new_state, status)

    def _grid_present(self) -> bool | None:
        """`None` when no grid relay is configured (not known). Configured but not `on`
        (off, unavailable, unknown) counts as the grid being absent: nothing is assumed."""
        entity = self.config.grid_relay_entity
        if entity is None:
            return None
        state = self.hass.states.get(entity)
        return state is not None and state.state == "on"

    def _report_floors(self, snapshot: Snapshot, readback: Readback, status: TickStatus
                       ) -> FloorPlan | None:
        """Planned slot floors per the floor table next to the actual ones (the executor
        writes them only with `manage_floors`)."""
        plan, hold = self._floor_plan(snapshot, status.morning)
        if plan is None:
            return None
        actual = list(readback.slot_capacity)
        status.floors = {
            "planned": list(plan.floors),
            "actual": actual,
            "kinds": list(plan.kinds),
            "hold": hold,
            "reasons": list(plan.reasons),
            "outlook": self._outlook,
            "managed": self.config.manage_floors,
            "diff": [i + 1 for i, (a, p) in enumerate(zip(actual, plan.floors, strict=True))
                     if a is None or abs(a - p) > 0.5],
        }
        return plan

    def _floor_plan(self, snapshot: Snapshot, outcome: MorningOutcome | None
                    ) -> tuple[FloorPlan | None, float | None]:
        params, state = self.config.floor, self.runtime.state
        if params is None or snapshot.slot_starts is None or state is None:
            return None, None
        now, tz = snapshot.now, dt_util.get_default_time_zone()
        intervals = schedule_intervals(self.hass, self.config, now)

        def is_nt_at(t: datetime) -> bool | None:
            return None if intervals is None else schedule_nt(intervals, t)

        profile, pv = self.profiles.profile, self.pv_forecasts.forecast
        reco = self.config.recovery
        hold = None
        if profile is not None and pv is not None and reco is not None:
            def p90(t: datetime) -> float | None:
                if not profile.horizon_start <= t < profile.horizon_end:
                    return None
                return profile.steps[int((t - profile.horizon_start) / STEP)].p90

            hold = hold_floor_pct(now=now, tz=tz, floor_vt_pct=params.floor_vt_pct,
                                  is_nt_at=is_nt_at, house_p90_kw=p90, pv_kw=pv.kw_at,
                                  c_use_kwh=reco.c_use_kwh, eta_d=reco.eta_d)
        grant = state.active_grant
        recovery = state.recovery
        target = None
        if grant is not None:
            target = grant.identity.target_pct
        elif recovery is not None and recovery.state == "cloudy_hold":
            target = recovery.hold_floor_pct
        entities = replace(self.config.slot_entities, slot_starts=snapshot.slot_starts)
        charging = current_slot(entities, snapshot.local_time) \
            if grant is not None and grant.status == "active" else None
        label, balance, why = "unknown", None, "no_recovery_params"
        if reco is not None:
            label, balance, why = outlook_status(
                profile, pv, now, _local_outlook(now, reco.outlook_days), reco.topology_epoch)
        self._outlook = {"label": label, "balance_kwh": balance, "why_unknown": why}
        ctx = FloorContext(
            now=now, tz=tz, slot_starts=snapshot.slot_starts, is_nt_at=is_nt_at,
            hold_floor_pct=hold, grant_target_pct=target, charging_slot=charging,
            phase=state.morning.phase, refill_floor_pct=state.morning.floor_state_pct,
            dip_window=None if outcome is None else outcome.dip_window,
            outlook_poor=label != "good",
        )
        return floor_plan(params, ctx), hold

    async def _plan_recovery(self, snapshot: Snapshot, readback: Readback,
                             status: TickStatus) -> bool:
        params = self.config.recovery
        if params is None:
            return True
        floor = None
        if snapshot.slot_starts is not None:
            entities = replace(self.config.slot_entities, slot_starts=snapshot.slot_starts)
            floor = readback.slot_capacity[current_slot(entities, snapshot.local_time)]
        now = snapshot.now
        inputs = RecoveryInputs(
            now=now,
            soc_pct=snapshot.soc_pct if snapshot.telemetry_valid else None,
            current_floor_pct=floor,
            reading=snapshot.reading,
            reading_epoch=snapshot.reading_epoch,
            pv=self.pv_forecasts.forecast,
            profile=self.profiles.profile,
            nt_steps=offpeak_steps(schedule_intervals(self.hass, self.config, now), now),
            floors=readback.slot_capacity,
            outlook=_local_outlook(now, params.outlook_days),
            live=status.live,
            managed_floors=self._managed_floors(snapshot, status),
            audit_full=self.outbox.full,
            grid_present=self._grid_present(),
        )
        state = self.runtime.state
        new_state, outcome = plan_recovery(state, inputs, params, new_id=lambda: uuid4().hex,
                                           policy_version=POLICY_VERSION)
        status.recovery = outcome
        return new_state == state or await self._commit(new_state, status)

    def _managed_floors(self, snapshot: Snapshot, status: TickStatus) -> tuple[float, ...] | None:
        """The table floors for the state in force right now — only while managing."""
        if not self.config.manage_floors:
            return None
        plan, _ = self._floor_plan(snapshot, status.morning)
        return None if plan is None else plan.floors

    async def _reconcile(self, snapshot: Snapshot, readback: Readback,
                         status: TickStatus) -> None:
        state = self.runtime.state
        result = reconcile(
            state,
            now=snapshot.now,
            observed_charge_enable=readback.charging_enabled,
            reading=snapshot.reading,
            reading_epoch=snapshot.reading_epoch,
            budget_factor=self.config.budget_factor,
        )
        status.result = f"reconcile:{result.state.reconcile_state.value}"
        status.reason = result.reason
        if result.require_charge_off:
            await self._stop_if_charging(readback, status, force=True)
        if result.state != state:
            await self._commit(result.state, status)

    async def _stop_if_charging(self, readback: Readback, status: TickStatus,
                                *, force: bool = False) -> None:
        if not force and readback.charging_enabled is False:
            return
        if status.live:
            status.writes += await self.executor.async_safety_stop()
        else:
            status.intents += stop_ops(self.config.slot_entities)

    async def _commit(self, new_state: StoreState, status: TickStatus) -> bool:
        try:
            await self.runtime.async_commit(new_state)
        except StoreNotDurableError as exc:
            status.error = f"state not persisted: {exc}"
            return False
        return True


def _local_outlook(now: datetime, days: int) -> tuple[datetime, datetime]:
    """Start of tomorrow to the end of the `days`-th local day after today, as a UTC
    [start, end) (23/25 h days on DST changes)."""
    today = dt_util.as_local(now).date()
    start = dt_util.start_of_local_day(today + timedelta(days=1))
    end = dt_util.start_of_local_day(today + timedelta(days=1 + days))
    return dt_util.as_utc(start), dt_util.as_utc(end)


def _mqtt_publisher(hass: HomeAssistant):
    async def publish(topic: str, payload: str) -> None:
        from homeassistant.components import mqtt

        await mqtt.async_publish(hass, topic, payload, qos=1, retain=False)

    return publish
