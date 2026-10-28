"""Recovery episodes below the reserve (pure, no HA).

* Entry: SOC below the reserve while the current slot floor is at least the
  reserve — a lower floor means the configuration deliberately lets the
  battery go lower (morning exception), which must not trigger grid recovery.
* Each cycle without a grant: `recovery_decision` against the anchored
  `wait_until`. PV is credited only with a usable house load profile
  (`wait_pv_blocker`); otherwise the surplus is taken as zero, so the
  conservative branch (grid recovery in off-peak) applies.
* `grid_recovery` prepares the grant in the same episode, persisted before
  anything enables. No off-peak window known = no grant yet, reason recorded.
* With a grant: an expired window is extended over future off-peak steps for
  the remaining budget, same ID. Without a grant the episode ends once the
  reserve is restored.
* After the grant closed (`floor_after_recovery`): every cycle decides anew.
  `cloudy_hold` keeps the floors at the grant target while the outlook's
  corrected PV minus the house load is negative — or cannot be judged (no
  usable profile/forecast); otherwise `restoring` writes the restore floors
  (the floors found when the grant was prepared, never below the reserve —
  a lower floor needs the explicit morning exception, not a found value).
  Losing the reason (inputs gone, cloudy again) returns to `cloudy_hold`,
  which raises any already-restored floor back. A floor that is neither the
  hold nor the restore value was changed from outside: nothing is written,
  the episode holds with `floor_conflict`. Unknown saved floors = hold.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .energy_core.grant import CounterReading
from .energy_core.house_load import HouseLoadProfile, wait_pv_blocker
from .energy_core.pv_forecast import PvForecast, pv_blocker
from .energy_core.recovery import STEP_MINUTES, recovery_decision
from .lifecycle import cancel_prepared_grant, extend_window, prepare_grant
from .slot_plan import CAPACITY_TOLERANCE_PCT
from .state_schema import RecoveryEpisode, StoreState

STEP = timedelta(minutes=STEP_MINUTES)


@dataclass(frozen=True)
class RecoveryParams:
    reserve_pct: float
    recovery_target_pct: float
    recovery_pv_wait_h: float
    c_use_kwh: float
    eta_c: float
    eta_d: float
    e_err_morning_kwh: float
    assumed_charge_rate_dc_kw: float
    topology_epoch: int
    outlook_days: int = 2  # local days after today that the outlook looks at


@dataclass(frozen=True)
class RecoveryInputs:
    now: datetime
    soc_pct: float | None  # None = telemetry invalid
    current_floor_pct: float | None
    reading: CounterReading
    reading_epoch: str
    pv: PvForecast | None  # snapshot with the correction already applied; None = none received
    profile: HouseLoadProfile | None
    nt_steps: Sequence[datetime]  # future off-peak step starts per the schedule
    floors: tuple[float | None, ...] = ()  # current slot floors (readback)
    # outlook window: start of tomorrow (local) to the end of the last day looked at, UTC
    outlook: tuple[datetime, datetime] | None = None
    live: bool = True  # shadow: floors were never raised, so they are not compared
    # slot floors planned by the floor table for the CURRENT state; set only while the
    # integration manages the floors (then it, not saved floors, is the authority)
    managed_floors: tuple[float, ...] | None = None
    audit_full: bool = False  # the audit outbox is full: no NEW grant can be recorded
    # False = the grid is known to be absent (island or the relay unavailable): no grid
    # grant is created then. None = not known/not configured: no restriction.
    grid_present: bool | None = None


@dataclass(frozen=True)
class RecoveryOutcome:
    action: str  # none | entered | wait_pv | grant_prepared | no_window | window_extended
    #              | hold | restoring | ended
    reason: str | None = None
    pv_blocker: str | None = None


def _blocker(inputs: RecoveryInputs, required_end: datetime,
             params: RecoveryParams) -> str | None:
    """Both the house load profile and the PV forecast must cover [now, required_end)."""
    return wait_pv_blocker(
        inputs.profile, now=inputs.now, required_end=required_end,
        topology_epoch=params.topology_epoch,
    ) or pv_blocker(
        inputs.pv, now=inputs.now, required_end=required_end,
        topology_epoch=params.topology_epoch,
    )


def _pv_kw(forecast: PvForecast) -> Callable[[datetime], float]:
    def pv(t: datetime) -> float:
        kw = forecast.kw_at(t)
        return 0.0 if kw is None else kw  # outside the forecast nothing is credited

    return pv


def _surplus(inputs: RecoveryInputs, required_end: datetime,
             params: RecoveryParams) -> tuple[Callable[[datetime], float], str | None]:
    blocker = _blocker(inputs, required_end, params)
    if blocker is not None:
        return (lambda _t: 0.0), blocker
    profile = inputs.profile
    assert profile is not None and inputs.pv is not None
    pv = _pv_kw(inputs.pv)

    def surplus(t: datetime) -> float:
        house = profile.p50_at(t)
        return -math.inf if house is None else pv(t) - house  # EV: none planned yet

    return surplus, None


def _window_end(steps: Sequence[datetime], needed: int) -> datetime | None:
    selected = list(steps)[:needed]
    return selected[-1] + STEP if selected else None


def _steps_needed(remaining_dc_kwh: float, params: RecoveryParams) -> int:
    per_step = params.assumed_charge_rate_dc_kw * STEP_MINUTES / 60
    return max(1, math.ceil(remaining_dc_kwh / per_step))


def plan_recovery(
    state: StoreState,
    inputs: RecoveryInputs,
    params: RecoveryParams,
    *,
    new_id: Callable[[], str],
    policy_version: str,
) -> tuple[StoreState, RecoveryOutcome]:
    now = inputs.now
    if state.provisioning_state != "ready" or state.reconcile_state.value != "ready":
        return state, RecoveryOutcome("none", "not_ready")
    grant = state.active_grant
    if grant is not None:
        cancelled = _reassess_prepared(state, inputs, params, new_id)
        if cancelled is not None:
            return cancelled
        return _maintain_grant(state, inputs, params, now)

    recovery = state.recovery
    if recovery is not None and recovery.state in ("cloudy_hold", "restoring"):
        return _after_grant(state, recovery, inputs, params)
    soc = inputs.soc_pct
    if soc is None:
        return state, RecoveryOutcome("none", "telemetry_invalid")
    if recovery is not None and soc >= params.reserve_pct:
        return replace(state, state_revision=state.state_revision + 1, recovery=None), \
            RecoveryOutcome("ended", "reserve_restored")
    if soc >= params.reserve_pct:
        return state, RecoveryOutcome("none")

    if inputs.grid_present is False:
        return state, RecoveryOutcome("none", "island_or_grid_unknown")
    action = "wait_pv"
    if recovery is None and state.morning.phase != "none":
        # D-50: below the reserve inside the morning exception is not a recovery trigger
        return state, RecoveryOutcome("none", f"morning_{state.morning.phase}")
    if recovery is None:
        floor = inputs.current_floor_pct
        # transitional rule while floors are not managed: a floor below the reserve is
        # a deliberate dip. Managed: only the morning phase (checked above) lowers it.
        if inputs.managed_floors is None and (floor is None or floor < params.reserve_pct):
            return state, RecoveryOutcome("none", "floor_below_reserve")
        recovery = RecoveryEpisode(
            episode_id=new_id(),
            entered_at=now,
            wait_until=now + timedelta(hours=params.recovery_pv_wait_h),
            state="wait_pv",
            decided_at=now,
        )
        action = "entered"

    surplus, blocker = _surplus(inputs, recovery.wait_until, params)
    if not inputs.reading.available or inputs.reading_epoch != state.counter_state.counter_epoch:
        return _with_recovery(state, replace(recovery, decided_at=now,
                                             reason_code="counter_unavailable")), \
            RecoveryOutcome(action, "counter_unavailable", blocker)
    decision = recovery_decision(
        grant_id=new_id(),
        now=now,
        soc_now_pct=soc,
        reserve_pct=params.reserve_pct,
        recovery_target_pct=params.recovery_target_pct,
        recovery_pv_wait_h=params.recovery_pv_wait_h,
        recovery_wait_until=recovery.wait_until,
        c_use_kwh=params.c_use_kwh,
        eta_c=params.eta_c,
        eta_d=params.eta_d,
        e_err_morning_kwh=params.e_err_morning_kwh,
        pv_surplus_ac_kw=surplus,
        counter_id=inputs.reading.counter_id,
        checkpoint_dc_kwh=inputs.reading.raw_kwh,
        candidate_nt_steps=list(inputs.nt_steps),
        assumed_charge_rate_dc_kw=params.assumed_charge_rate_dc_kw,
    )
    if decision.state == "wait_pv":
        return _with_recovery(state, replace(recovery, state="wait_pv", decided_at=now,
                                             reason_code=None)), \
            RecoveryOutcome(action if action == "entered" else "wait_pv", None, blocker)
    assert decision.grant is not None
    if decision.grant.window.end <= now:
        return _with_recovery(state, replace(recovery, state="wait_pv", decided_at=now,
                                             reason_code="no_offpeak_window")), \
            RecoveryOutcome("no_window", "no_offpeak_window", blocker)

    if inputs.audit_full:
        # a grant whose decisions cannot be recorded locally is not started (13: the
        # audit never blocks protections, but it does gate NEW authorizations)
        return _with_recovery(state, replace(recovery, state="wait_pv", decided_at=now,
                                             reason_code="audit_outbox_full")), \
            RecoveryOutcome("blocked", "audit_outbox_full", blocker)

    prepared = prepare_grant(
        replace(state, recovery=None),
        grant=decision.grant,
        episode_id=recovery.episode_id,
        counter_epoch=inputs.reading_epoch,
        now=now,
        policy_version=policy_version,
        deadline_at=recovery.wait_until,
    )
    floors = inputs.floors
    restore = (tuple(max(f, params.reserve_pct) for f in floors)
               if floors and None not in floors else None)
    # floors saved by an earlier grant of this episode are the ones to go back to
    episode = replace(recovery, state="grid_recovery", decided_at=now, reason_code=blocker,
                      restore_floors=recovery.restore_floors or restore,
                      hold_floor_pct=decision.grant.identity.target_pct)
    return replace(prepared, recovery=episode), RecoveryOutcome("grant_prepared", None, blocker)


def _with_recovery(state: StoreState, recovery: RecoveryEpisode) -> StoreState:
    if state.recovery == recovery:
        return state
    return replace(state, state_revision=state.state_revision + 1, recovery=recovery)


def _reassess_prepared(
    state: StoreState, inputs: RecoveryInputs, params: RecoveryParams,
    new_id: Callable[[], str],
) -> tuple[StoreState, RecoveryOutcome] | None:
    """A grant that was prepared but never enabled is re-judged every cycle by the very
    function that created it, against the SAME anchored deadline. A grant born without
    usable inputs (the conservative default) therefore does not outlive the data that
    arrive later: once the PV can restore the reserve in time — or the reserve is
    already restored — it is cancelled. Anything doubtful keeps it."""
    grant, recovery = state.active_grant, state.recovery
    soc = inputs.soc_pct
    if (grant is None or grant.status != "prepared" or state.pending_step is not None
            or recovery is None or recovery.state != "grid_recovery" or soc is None):
        return None
    now = inputs.now
    if soc >= params.reserve_pct:
        return (cancel_prepared_grant(state, now=now, reason="reserve_restored",
                                      new_episode_id=None),
                RecoveryOutcome("grant_cancelled", "reserve_restored"))
    if now >= recovery.wait_until:
        return None  # the wait is over: the grid grant stands
    surplus, blocker = _surplus(inputs, recovery.wait_until, params)
    if blocker is not None:
        return None  # not judgeable: keep the conservative grant
    decision = recovery_decision(
        grant_id=new_id(), now=now, soc_now_pct=soc, reserve_pct=params.reserve_pct,
        recovery_target_pct=params.recovery_target_pct,
        recovery_pv_wait_h=params.recovery_pv_wait_h, recovery_wait_until=recovery.wait_until,
        c_use_kwh=params.c_use_kwh, eta_c=params.eta_c, eta_d=params.eta_d,
        e_err_morning_kwh=params.e_err_morning_kwh, pv_surplus_ac_kw=surplus,
        counter_id=grant.identity.counter_id, checkpoint_dc_kwh=grant.identity.checkpoint_dc_kwh,
        candidate_nt_steps=(), assumed_charge_rate_dc_kw=params.assumed_charge_rate_dc_kw,
    )
    if decision.state != "wait_pv":
        return None
    return (cancel_prepared_grant(state, now=now, reason="pv_restores_reserve",
                                  new_episode_id=new_id()),
            RecoveryOutcome("grant_cancelled", "pv_restores_reserve"))


def _maintain_grant(state: StoreState, inputs: RecoveryInputs, params: RecoveryParams,
                    now: datetime) -> tuple[StoreState, RecoveryOutcome]:
    grant = state.active_grant
    assert grant is not None
    if grant.status == "closing" or now < grant.window.end:
        return state, RecoveryOutcome("none")
    remaining = max(0.0, grant.identity.budget_dc_kwh - grant.usage.used_dc_kwh)
    future = [t for t in inputs.nt_steps if t >= now]
    new_end = _window_end(future, _steps_needed(remaining, params))
    if new_end is None or new_end <= grant.window.end:
        return state, RecoveryOutcome("none", "no_offpeak_window")
    return extend_window(state, new_end=new_end, now=now), RecoveryOutcome("window_extended")


def outlook_status(
    profile: HouseLoadProfile | None, pv: PvForecast | None, now: datetime,
    window: tuple[datetime, datetime] | None, topology_epoch: int,
) -> tuple[str, float | None, str | None]:
    """`("good" | "poor" | "unknown", balance_kwh, why_unknown)`: corrected PV minus the
    house load P50 over the whole outlook window. Unknown = a snapshot missing, stale,
    not covering the window or of another topology; callers treat it like poor."""
    if window is None:
        return "unknown", None, "outlook_unknown"
    blocker = (wait_pv_blocker(profile, now=now, required_end=window[1],
                               topology_epoch=topology_epoch)
               or pv_blocker(pv, now=now, required_end=window[1],
                             topology_epoch=topology_epoch))
    if blocker is not None:
        return "unknown", None, blocker
    assert pv is not None and profile is not None
    balance = outlook_balance_kwh(_pv_kw(pv), profile, *window)
    if balance == -math.inf:
        return "unknown", None, "profile_not_covering_window"
    return ("good" if balance >= 0 else "poor"), balance, None


def outlook_balance_kwh(pv: Callable[[datetime], float], profile: HouseLoadProfile,
                         start: datetime, end: datetime) -> float:
    """Corrected PV minus house load (P50) over [start, end), kWh AC."""
    total, t = 0.0, start
    while t < end:
        house = profile.p50_at(t)
        if house is None:
            return -math.inf
        total += (pv(t) - house) * STEP_MINUTES / 60
        t += STEP
    return total


def floor_writes(recovery: RecoveryEpisode,
                 floors: Sequence[float | None]) -> list[tuple[int, float]] | None:
    """Slot floors to write for an episode after its grant (index, value);
    `None` = conflict (a floor that is neither the hold nor the restore value,
    or unreadable) — then nothing may be written. Shared by the decision and
    the executor so neither writes over an outside change."""
    restore, hold = recovery.restore_floors, recovery.hold_floor_pct
    if restore is None or hold is None or len(floors) != len(restore):
        return None
    writes = []
    for i, (current, back) in enumerate(zip(floors, restore, strict=True)):
        # a wait_pv episode that follows a cancelled grant puts its floors back too
        target = back if recovery.state in ("restoring", "wait_pv") else max(hold, back)
        known = (hold, back)
        if current is None or not any(abs(current - k) <= CAPACITY_TOLERANCE_PCT
                                      for k in (*known, target)):
            return None
        if abs(current - target) > CAPACITY_TOLERANCE_PCT:
            writes.append((i, target))
    return writes


def _floors_match(actual: Sequence[float | None], planned: Sequence[float] | None) -> bool:
    return planned is not None and len(actual) == len(planned) and all(
        a is not None and abs(a - p) <= CAPACITY_TOLERANCE_PCT
        for a, p in zip(actual, planned, strict=True))


def _hold(state: StoreState, recovery: RecoveryEpisode, reason: str, now: datetime,
          blocker: str | None = None) -> tuple[StoreState, RecoveryOutcome]:
    decided = recovery.decided_at if recovery.state == "cloudy_hold" else now
    held = replace(recovery, state="cloudy_hold", reason_code=reason, decided_at=decided)
    return _with_recovery(state, held), RecoveryOutcome("hold", reason, blocker)


def _after_grant(state: StoreState, recovery: RecoveryEpisode, inputs: RecoveryInputs,
                 params: RecoveryParams) -> tuple[StoreState, RecoveryOutcome]:
    now = inputs.now
    managed = inputs.managed_floors is not None
    if recovery.hold_floor_pct is None or (recovery.restore_floors is None and not managed):
        return _hold(state, recovery, "floors_unknown", now)
    if not managed and inputs.live and floor_writes(recovery, inputs.floors) is None:
        return _hold(state, recovery, "floor_conflict", now)

    label, _balance, why = outlook_status(inputs.profile, inputs.pv, now, inputs.outlook,
                                          params.topology_epoch)
    if label == "unknown":
        return _hold(state, recovery, why or "outlook_unknown", now, why)
    if label == "poor":
        return _hold(state, recovery, "cloudy", now)

    restoring = replace(recovery, state="restoring", reason_code=None,
                        decided_at=now if recovery.state != "restoring" else recovery.decided_at)
    if managed:
        # the plan was computed for the state in force when this cycle began: it is the
        # normal table only if the episode was already restoring
        done = recovery.state == "restoring" and (
            not inputs.live or _floors_match(inputs.floors, inputs.managed_floors))
    else:
        done = (recovery.state == "restoring" if not inputs.live  # shadow: reported once
                else floor_writes(restoring, inputs.floors) == [])
    if done:
        return replace(state, state_revision=state.state_revision + 1, recovery=None), \
            RecoveryOutcome("ended", "floors_restored")
    return _with_recovery(state, restoring), RecoveryOutcome("restoring")
