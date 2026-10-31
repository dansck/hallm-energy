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
from datetime import date, datetime, timedelta
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .audit_outbox import AUDIT_CAPACITY, AuditOutbox
from .config import EnergyConfig
from .const import POLICY_VERSION
from .device_io import DeviceIO, WriteResult
from .energy_core.ev_plan import MIN_POWER_KW, TariffFacts
from .energy_core.ev_sun import first_sun_day
from .ev_executor import EvExecutor
from .ev_flow import EvShadow
from .ev_limit import CarLimitWriter
from .executor import Gates, GrantExecutor, Snapshot
from .floor_table import FloorContext, FloorPlan, floor_plan, hold_floor_pct
from .guard_flow import GuardShadow
from .inputs import (
    build_inputs,
    offpeak_hours_between,
    offpeak_steps,
    schedule_intervals,
    schedule_nt,
)
from .lifecycle import (
    GrantTransitionError,
    add_audit_events,
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
from .start_gate import StartGate
from .state_schema import StoreState
from .store import StoreNotDurableError

_LOGGER = logging.getLogger(__name__)
STEP = timedelta(minutes=15)


class ProvisionError(Exception):
    pass


EV_AUDIT_RESERVE = 100  # outbox places the car never uses: the grants keep them
EV_DECISION_EVENTS_PER_HOUR = 20
GRID_OUTAGE_AFTER = timedelta(minutes=1)      # a flicker of the relay is not an outage
GRID_VOLTAGE_AFTER = timedelta(seconds=30)    # an excursion that lasts, not a single sample
GRID_VOLTAGE_MIN_V, GRID_VOLTAGE_MAX_V = 210.0, 245.0
INVERTER_GONE_AFTER = timedelta(minutes=5)    # the BMS/inverter channel is gone


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
        self.start_gate = StartGate()
        self.executor = GrantExecutor(runtime, self.io, config.slot_entities, self.gates,
                                      lowering_recheck=self._lowering_inputs_trusted,
                                      budget_factor=config.budget_factor,
                                      start_gate=self.start_gate)
        self.status = TickStatus()
        self.guard = GuardShadow(config)
        self.guard_report: dict | None = None  # report only: nothing is written
        self._guard_failing = False
        self.ev = EvShadow(hass, config)
        self.ev_exec = EvExecutor(
            DeviceIO(hass, call_timeout_s=10.0, readback_timeout_s=10.0), config, self.gates,
            grid_busy=self._grid_charging_busy, start_gate=self.start_gate,
            audit=self.queue_ev_audit)
        self.ev_report: dict | None = None  # report only: nothing is written
        self.ev_limit = CarLimitWriter(hass, config, self.gates, audit=self.queue_ev_audit)
        self._ev_audit_key: tuple | None = None
        self._ev_audit_times: list[datetime] = []
        self.ev_audit_dropped = 0
        self.ev_drive_tomorrow = "unknown"  # the owner's answer, asked by the assistant
        self._drive_for: date | None = None  # the day the answer is about
        self.ev_force = False  # "must charge": set by a switch, restored by it after a restart
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
                await self._step(
                    replace(inputs.snapshot, hold_reason=self._grant_hold_reason()), status)
            except Exception as exc:  # the loop must survive; nothing enables after it
                _LOGGER.exception("tick failed")
                status.result, status.error = "error", repr(exc)
            self.status = status
            self._notify()
        await self._observe_conditions(status)  # outside the control lock: never delays a cycle
        await self._observe_ev(now)
        return status

    def guard_step(self, now: datetime | None = None) -> None:
        """One phase-guard evaluation, on its own fast timer. Report only; listeners (the status
        sensor) are told only when the decision changes, not on every new reading."""
        now = now or dt_util.utcnow()
        try:
            report = self.guard.evaluate(self.hass, self.config, now, self._ev_charging())
        except Exception:  # a report must never stop anything else
            if not self._guard_failing:
                _LOGGER.exception("phase guard report failed")
            self._guard_failing = True
            return
        self._guard_failing = False
        changed = _guard_decision(report) != _guard_decision(self.guard_report)
        self.guard_report = report
        if changed:
            self._notify()
            if self._grant_hold_reason() is not None and self._grid_charging_busy():
                # grid charging has to stop now, not at the next tick
                self.hass.async_create_background_task(
                    self.async_tick(), "hallm_energy guard hold")

    def _ev_charging(self) -> bool:
        """The car is plugged in and the wallbox is not paused: it may be drawing power."""
        cfg = self.config
        if not cfg.ev_connected_entity:
            return False
        plugged = self.hass.states.get(cfg.ev_connected_entity)
        paused = self.hass.states.get(cfg.ev_wallbox_pause_entity) if cfg.ev_wallbox_pause_entity \
            else None
        return plugged is not None and plugged.state == "on" and (
            paused is None or paused.state != "on")

    def _grant_hold_reason(self) -> str | None:
        """Why grid charging may not run right now although a grant allows it: the phase guard
        forbids it, or its measurements are gone. Without a configured guard nothing holds."""
        report = self.guard_report
        if not self.config.phase_current_entities or report is None:
            return None
        if report.get("stale"):
            return "guard_stale"
        if not report["grid_charge_allowed"]:
            return "guard_phase_overload"
        return None

    def _tariff_facts(self, now: datetime, deadline: datetime | None) -> TariffFacts:
        """Off-peak now needs the relay and the schedule to agree; off-peak time until a deadline
        comes from the schedule alone."""
        intervals = schedule_intervals(self.hass, self.config, now)
        relay = self.hass.states.get(self.config.hdo_relay_entity)
        relay_nt = None
        if relay is not None and relay.state in ("on", "off"):
            relay_nt = relay.state == "on"
        planned = None if intervals is None else schedule_nt(intervals, now)
        if relay_nt is None:
            nt_now = None
        elif planned is None:
            nt_now = False if relay_nt is False else None  # the relay says off-peak, nobody vouches
        else:
            nt_now = relay_nt if planned == relay_nt else None
        hours, covered = (None, False) if deadline is None else offpeak_hours_between(
            intervals, now, deadline)
        return TariffFacts(nt_now, hours, covered)

    async def async_set_ev_force(self, on: bool) -> None:
        """The "must charge" wish. The report follows at once, not on the next tick."""
        self.ev_force = on
        await self._observe_ev(dt_util.utcnow())
        self._notify()

    def _grid_charging_busy(self) -> bool:
        """The house battery is being charged from the grid, or a grant is on its way to."""
        state = self.runtime.state
        if state is not None and (
                state.pending_step is not None
                or (state.active_grant is not None and state.active_grant.status == "active")):
            return True
        return self.io.read(self.config.grid_charge_entity) is not False

    def queue_ev_audit(self, payloads: list[dict]) -> None:
        """The car's writes and decisions go into the same durable audit as the grants'. Recorded
        in the background under the control lock, so the wallbox is never held up by it."""
        if payloads:
            self.hass.async_create_background_task(
                self._record_ev_audit(payloads), "hallm_energy EV audit")

    async def _record_ev_audit(self, payloads: list[dict]) -> None:
        async with self._lock:
            state = self.runtime.state
            if state is None or self.runtime.blocked_reason is not None:
                self.ev_audit_dropped += len(payloads)
                return
            now = dt_util.utcnow()
            events = [(f"{p['kind']}:{uuid4().hex}", p) for p in payloads]
            new = add_audit_events(state, events, now, limit=AUDIT_CAPACITY - EV_AUDIT_RESERVE)
            if new is state:
                self.ev_audit_dropped += len(payloads)
                return
            try:
                await self.runtime.async_commit(new)
            except StoreNotDurableError as exc:
                _LOGGER.warning("EV audit not persisted: %s", exc)
                self.ev_audit_dropped += len(payloads)

    def _audit_ev_decision(self, report: dict | None, now: datetime) -> None:
        """One event when what the car is doing and why changes (not on every reading), at most a
        handful per hour: a flapping input must not flood the audit."""
        if report is None:
            return
        gate = report.get("grid_charge") or {}
        key = (report.get("state"), report.get("reason"), gate.get("allowed"),
               report.get("force_requested"), report.get("deye_mode_required"))
        if key == self._ev_audit_key:
            return
        self._ev_audit_key = key
        self._ev_audit_times = [t for t in self._ev_audit_times if now - t < timedelta(hours=1)]
        if len(self._ev_audit_times) >= EV_DECISION_EVENTS_PER_HOUR:
            self.ev_audit_dropped += 1
            return
        self._ev_audit_times.append(now)
        request = report.get("request") or {}
        self.queue_ev_audit([{
            "kind": "ev_decision", "ev_state": report.get("state"), "reason": report.get("reason"),
            "soc_pct": report.get("soc_pct"), "soc_reason": report.get("soc_reason"),
            "target_pct": report.get("effective_target_pct"),
            "force": report.get("force_requested"),
            "grid_charge": gate or None,
            "deye_mode_required": report.get("deye_mode_required"),
            "request": request or None, "drive_tomorrow": report.get("drive_tomorrow"),
            "sun_window_day": (report.get("sun") or {}).get("first_window_day"),
            "withheld": report.get("intents_withheld")}])

    def _ev_report(self, now: datetime) -> dict | None:
        return self.ev.evaluate(
            now, self.guard_report, self._grid_present(), self.ev_force,
            lambda deadline: self._tariff_facts(now, deadline), self._sun_outlook(now),
            self.drive_answer(now), self._grid_charging_busy())

    def drive_answer(self, now: datetime) -> str:
        """The owner's answer about driving tomorrow while it still concerns tomorrow; after
        midnight it is spent and the answer is `unknown` again."""
        if self._drive_for != dt_util.as_local(now).date() + timedelta(days=1):
            return "unknown"
        return self.ev_drive_tomorrow

    async def async_set_ev_drive_tomorrow(self, answer: str) -> None:
        if answer not in ("unknown", "yes", "no"):
            raise ValueError(f"not an answer: {answer}")
        now = dt_util.utcnow()
        self.ev_drive_tomorrow = answer
        self._drive_for = dt_util.as_local(now).date() + timedelta(days=1)
        await self._observe_ev(now)
        self._notify()

    def restore_ev_drive_tomorrow(self, answer: str, given_at: datetime) -> None:
        """After a restart: an answer given on day X still counts for X + 1."""
        if answer in ("yes", "no"):
            self.ev_drive_tomorrow = answer
            self._drive_for = dt_util.as_local(given_at).date() + timedelta(days=1)

    def _sun_outlook(self, now: datetime) -> tuple[int | None, float, bool] | None:
        """The first day with sun worth waiting for, from the PV forecast and the house profile;
        `None` when either is missing (then nobody waits for sun that cannot be vouched for)."""
        pv, profile = self.pv_forecasts.forecast, self.profiles.profile
        cfg = self.config
        if pv is None or profile is None or cfg.recovery is None:
            return None
        need_kwh = cfg.ev_daily_trip_pct * cfg.ev_ac_kwh_per_pct
        return first_sun_day(
            pv.kw_at, profile.p50_at, start=now, days=cfg.recovery.outlook_days,
            tz=dt_util.get_default_time_zone(), min_surplus_kw=MIN_POWER_KW, min_kwh=need_kwh,
            skip_days=(1,) if self.drive_answer(now) == "yes" else ())

    async def _observe_ev(self, now: datetime) -> None:
        """The EV report and, when the gate is live, its carrying out. Outside the control lock
        too: a slow calendar or wallbox never delays a grant cycle."""
        if not self.ev.enabled:
            return
        try:
            await self.ev.async_refresh_calendar(now)
            report = self._ev_report(now)
        except Exception:
            _LOGGER.exception("EV report failed")
            return
        changed = _ev_decision(report) != _ev_decision(self.ev_report)
        self.ev_report = report
        self._audit_ev_decision(report, now)
        if changed:
            self._notify()
        if report is not None and report.get("car_limit_desired") is not None:
            self.ev_limit.request(report["car_limit_desired"], now)
        if report is not None and report.get("intents"):
            before = (self.ev_exec.last.result, self.ev_exec.last.reason)
            try:
                await self.ev_exec.async_apply(
                    report, now=now, recheck=lambda: self._ev_report(dt_util.utcnow()))
            except Exception:
                _LOGGER.exception("EV write failed")
            if (self.ev_exec.last.result, self.ev_exec.last.reason) != before:
                self._notify()

    async def async_ev_cycle(self) -> None:
        """One fast EV cycle (every second): the guard first, then the EV plan and its writes."""
        self.guard_step()
        await self._observe_ev(dt_util.utcnow())

    async def _observe_conditions(self, status: TickStatus) -> None:
        """Owner alerts (`notify_service`) for CRITICAL events only: the grid is gone, or the
        inverter cannot be reached. Everything else is handled by the integration itself."""
        notifier = self.notifier
        if not notifier.enabled:
            return
        now = status.at
        grid_gone = self._grid_present() is False
        inverter_gone = ("soc_unavailable" in status.problems
                         or self.io.read_slots(self.config.slot_entities).grid_charge is None)
        await notifier.async_observe(
            "grid_outage", grid_gone,
            "Grid outage: the grid relay is off or unavailable. The integration does no grid "
            "charging until the grid is back.", now, after=GRID_OUTAGE_AFTER)
        bad_voltage = self._abnormal_grid_voltage()
        await notifier.async_observe(
            "grid_voltage", bad_voltage is not None,
            f"Grid voltage out of range: {bad_voltage}. Check the supply "
            f"({GRID_VOLTAGE_MIN_V:.0f}-{GRID_VOLTAGE_MAX_V:.0f} V is normal).", now,
            after=GRID_VOLTAGE_AFTER)
        await notifier.async_observe(
            "inverter_unreachable", inverter_gone,
            "The inverter cannot be reached: battery SOC or the charge settings are unavailable. "
            "The integration changes nothing until they are back.", now, after=INVERTER_GONE_AFTER)

    def _abnormal_grid_voltage(self) -> str | None:
        """`"<entity>: <V> V"` of the first phase outside the normal range, else `None`; an
        unavailable sensor is not a voltage problem (the inverter check covers it)."""
        for entity in self.config.grid_voltage_entities:
            state = self.hass.states.get(entity)
            try:
                volts = float(state.state) if state is not None else None
            except (TypeError, ValueError):
                volts = None
            if volts is not None and not GRID_VOLTAGE_MIN_V <= volts <= GRID_VOLTAGE_MAX_V:
                return f"{entity}: {volts:g} V"
        return None

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
        return await self.executor.async_safety_stop()

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
            "live": self.config.manage_floors and self.gates.floors_live(),
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


def _guard_decision(report: dict | None) -> tuple | None:
    if report is None:
        return None
    return (report["ev_current_cap_a"], report["ev_paused"], report["grid_charge_allowed"],
            report["export_cap_w"], report["restricted"])


def _ev_decision(report: dict | None) -> tuple | None:
    if report is None:
        return None
    request = report.get("request") or {}
    gate = report.get("grid_charge") or {}
    return (report["state"], report["reason"], report.get("effective_target_pct"),
            gate.get("allowed"),
            request.get("deadline_at"), request.get("feasible"), report.get("force_requested"),
            tuple((i["target"], i["value"]) for i in report.get("intents", ())))


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
