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
* After the grant closed (`floor_after_recovery`): `cloudy_hold` keeps the
  floors at the target while tomorrow's corrected PV minus the house load is
  negative — or cannot be judged (no usable profile/forecast); otherwise
  `restoring` writes back the slot floors saved when the grant was prepared,
  and the episode ends once they read back. Unknown saved floors = hold.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .energy_core.grant import CounterReading
from .energy_core.house_load import HouseLoadProfile, wait_pv_blocker
from .energy_core.recovery import STEP_MINUTES, recovery_decision
from .lifecycle import extend_window, prepare_grant
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


@dataclass(frozen=True)
class RecoveryInputs:
    now: datetime
    soc_pct: float | None  # None = telemetry invalid
    current_floor_pct: float | None
    reading: CounterReading
    reading_epoch: str
    pv_ac_kw: Callable[[datetime], float] | None  # corrected (P10) forecast; None = unusable
    profile: HouseLoadProfile | None
    nt_steps: Sequence[datetime]  # future off-peak step starts per the schedule
    floors: tuple[float | None, ...] = ()  # current slot floors (readback)
    tomorrow: tuple[datetime, datetime] | None = None  # local tomorrow as UTC [start, end)


@dataclass(frozen=True)
class RecoveryOutcome:
    action: str  # none | entered | wait_pv | grant_prepared | no_window | window_extended
    #              | hold | restoring | ended
    reason: str | None = None
    pv_blocker: str | None = None


def _surplus(inputs: RecoveryInputs, required_end: datetime,
             params: RecoveryParams) -> tuple[Callable[[datetime], float], str | None]:
    blocker = wait_pv_blocker(inputs.profile, now=inputs.now, required_end=required_end,
                              topology_epoch=params.topology_epoch)
    if blocker is None and inputs.pv_ac_kw is None:
        blocker = "pv_forecast_unusable"
    if blocker is not None:
        return (lambda _t: 0.0), blocker
    profile, pv = inputs.profile, inputs.pv_ac_kw
    assert profile is not None and pv is not None

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

    action = "wait_pv"
    if recovery is None:
        floor = inputs.current_floor_pct
        if floor is None or floor < params.reserve_pct:
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
    floors_before = tuple(floors) if floors and None not in floors else None
    episode = replace(recovery, state="grid_recovery", decided_at=now, reason_code=blocker,
                      floors_before=floors_before)
    return replace(prepared, recovery=episode), RecoveryOutcome("grant_prepared", None, blocker)


def _with_recovery(state: StoreState, recovery: RecoveryEpisode) -> StoreState:
    if state.recovery == recovery:
        return state
    return replace(state, state_revision=state.state_revision + 1, recovery=recovery)


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


def tomorrow_balance_kwh(pv: Callable[[datetime], float], profile: HouseLoadProfile,
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


def _after_grant(state: StoreState, recovery: RecoveryEpisode, inputs: RecoveryInputs,
                 params: RecoveryParams) -> tuple[StoreState, RecoveryOutcome]:
    now = inputs.now
    before = recovery.floors_before
    if before is None:
        return _with_recovery(state, replace(recovery, state="cloudy_hold",
                                             reason_code="floors_unknown")), \
            RecoveryOutcome("hold", "floors_unknown")
    if recovery.state == "restoring":
        current = inputs.floors
        if len(current) == len(before) and all(
            c is not None and abs(c - b) <= CAPACITY_TOLERANCE_PCT
            for c, b in zip(current, before, strict=True)
        ):
            return replace(state, state_revision=state.state_revision + 1, recovery=None), \
                RecoveryOutcome("ended", "floors_restored")
        return state, RecoveryOutcome("restoring")

    blocker = None
    if inputs.tomorrow is None:
        blocker = "tomorrow_unknown"
    else:
        blocker = wait_pv_blocker(inputs.profile, now=now, required_end=inputs.tomorrow[1],
                                  topology_epoch=params.topology_epoch)
        if blocker is None and inputs.pv_ac_kw is None:
            blocker = "pv_forecast_unusable"
    if blocker is not None:
        return _with_recovery(state, replace(recovery, reason_code=blocker)), \
            RecoveryOutcome("hold", blocker, blocker)
    assert inputs.tomorrow is not None and inputs.pv_ac_kw is not None
    assert inputs.profile is not None
    balance = tomorrow_balance_kwh(inputs.pv_ac_kw, inputs.profile, *inputs.tomorrow)
    if balance < 0:
        return _with_recovery(state, replace(recovery, reason_code="cloudy")), \
            RecoveryOutcome("hold", "cloudy")
    return _with_recovery(state, replace(recovery, state="restoring", decided_at=now,
                                         reason_code=None)), RecoveryOutcome("restoring")
