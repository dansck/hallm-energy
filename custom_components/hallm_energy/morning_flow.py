"""Morning exception driven from the persisted state (pure, no HA).

Every cycle: find the PV window of the relevant day (first/last 15-minute step
in which the corrected PV forecast exceeds the house load P50), judge the
forecast and the profile for exactly that window, and call the shared state
machine `morning_exception_decision`. Anything doubtful — no window, no usable
forecast or profile, telemetry gone, the gate switched off, the grid gone or
unknown — ends the phase (floors go back up) and never starts one. A running phase
is judged by the same standard every cycle, not only when it is entered. The phase
is only ever ENTERED when `enable_morning_dip` is on (activation gate), the grid is
present and no recovery episode runs.

While a phase is active, SOC below the reserve does NOT start recovery
(`recovery_flow` checks `state.morning.phase`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, tzinfo

from .energy_core.house_load import HouseLoadProfile, wait_pv_blocker
from .energy_core.morning import MorningState, morning_exception_decision
from .energy_core.pv_forecast import PvForecast, pv_blocker
from .state_schema import StoreState

STEP = timedelta(minutes=15)


@dataclass(frozen=True)
class MorningParams:
    reserve_pct: float
    dip_floor_pct: float
    dip_h: float
    refill_margin_min: float
    c_use_kwh: float
    eta_c: float
    eta_d: float
    e_err_morning_kwh: float
    topology_epoch: int
    enable_morning_dip: bool


@dataclass(frozen=True)
class MorningInputs:
    now: datetime
    tz: tzinfo
    soc_pct: float | None
    profile: HouseLoadProfile | None
    pv: PvForecast | None
    grid_present: bool  # False = island or unknown
    in_recovery: bool


@dataclass(frozen=True)
class MorningOutcome:
    phase: str
    floor_pct: float | None = None
    dip_window: tuple[datetime, datetime] | None = None
    t_pv_start: datetime | None = None
    t_pv_end: datetime | None = None
    handoff: bool = False
    reason: str | None = None  # why nothing (more) is granted


def _day_window(profile: HouseLoadProfile, pv: PvForecast, day_start: datetime,
                tz: tzinfo, margin_min: float) -> tuple[datetime, datetime] | None:
    """[t_pv_start, t_pv_end] of the local day beginning at `day_start`."""
    day = day_start.astimezone(tz).date()
    end_of_day = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
    if profile.horizon_end < end_of_day or pv.horizon_end < end_of_day:
        return None  # a window cut by the end of the data is not a real window
    first = last = None
    t = day_start
    while t < end_of_day:
        kw, house = pv.kw_at(t), profile.p50_at(t)
        if kw is not None and house is not None and kw > house:
            first = first or t
            last = t
        t += STEP
    if first is None or last is None:
        return None
    t_end = last + STEP - timedelta(minutes=margin_min)
    return (first, t_end) if t_end > first else None


def pv_window(profile: HouseLoadProfile, pv: PvForecast, now: datetime, tz: tzinfo,
              margin_min: float, *, phase_active: bool
              ) -> tuple[datetime, datetime] | None:
    local = now.astimezone(tz)
    today = datetime.combine(local.date(), time(0), tzinfo=tz)
    today_window = _day_window(profile, pv, today, tz, margin_min)
    if today_window is not None and (now < today_window[1] or phase_active):
        return today_window
    return _day_window(profile, pv, today + timedelta(days=1), tz, margin_min)


def plan_morning(state: StoreState, inputs: MorningInputs, params: MorningParams
                 ) -> tuple[StoreState, MorningOutcome]:
    now = inputs.now
    current = state.morning
    if state.provisioning_state != "ready" or state.reconcile_state.value != "ready":
        return state, MorningOutcome(current.phase, reason="not_ready")

    def end_phase(reason: str) -> tuple[StoreState, MorningOutcome]:
        return _with_morning(state, MorningState("none")), MorningOutcome("none", reason=reason)

    # Judged every cycle, in a running phase too: switching the gate off, a grid that is
    # gone or unknown, telemetry lost all end the exception at once (floors go back up).
    if current.phase != "none" and not params.enable_morning_dip:
        return end_phase("gate_off")
    if current.phase != "none" and not inputs.grid_present:
        return end_phase("grid_absent_or_unknown")
    if inputs.soc_pct is None:
        return end_phase("telemetry_invalid")
    if state.active_grant is not None or state.recovery is not None:
        return end_phase("recovery_in_progress")
    if inputs.profile is None or inputs.pv is None:
        return end_phase("profile_or_forecast_missing")

    window = pv_window(inputs.profile, inputs.pv, now, inputs.tz, params.refill_margin_min,
                       phase_active=current.phase != "none")
    if window is None:
        return end_phase("no_pv_window")
    t_start, t_end = window
    blocker = (wait_pv_blocker(inputs.profile, now=now, required_end=t_end,
                               topology_epoch=params.topology_epoch)
               or pv_blocker(inputs.pv, now=now, required_end=t_end,
                             topology_epoch=params.topology_epoch))
    profile, pv = inputs.profile, inputs.pv

    def house_p90(t: datetime) -> float:
        step = profile.steps[int((t - profile.horizon_start) / STEP)] \
            if profile.horizon_start <= t < profile.horizon_end else None
        return 0.0 if step is None else step.p90

    def surplus(t: datetime) -> float:
        kw, house = pv.kw_at(t), profile.p50_at(t)
        return 0.0 if kw is None or house is None else kw - house

    decision = morning_exception_decision(
        state=current,
        now=now,
        soc_now_pct=inputs.soc_pct,
        t_pv_start=t_start,
        t_pv_end=t_end,
        reserve_pct=params.reserve_pct,
        morning_dip_floor_pct=params.dip_floor_pct,
        morning_dip_h=params.dip_h,
        c_use_kwh=params.c_use_kwh,
        eta_c=params.eta_c,
        eta_d=params.eta_d,
        e_err_morning_kwh=params.e_err_morning_kwh,
        forecast_retrieved_at=pv.retrieved_at,
        forecast_max_age_h=(pv.valid_until - pv.retrieved_at).total_seconds() / 3600,
        forecast_covers_window=blocker is None,
        enable_morning_dip=params.enable_morning_dip,
        is_island=not inputs.grid_present,
        entered_from_recovery=inputs.in_recovery,
        battery_covered_load_ac_kw=house_p90,
        pv_surplus_ac_kw=surplus,
    )
    window_dip = (t_start - timedelta(hours=params.dip_h), t_start)
    outcome = MorningOutcome(
        phase=decision.phase,
        floor_pct=decision.floor_pct,
        dip_window=window_dip if decision.phase == "morning_dip" else None,
        t_pv_start=t_start,
        t_pv_end=t_end,
        handoff=decision.handoff_to_recovery,
        reason=blocker,
    )
    return _with_morning(state, decision.next_state), outcome


def _with_morning(state: StoreState, morning: MorningState) -> StoreState:
    if state.morning == morning:
        return state
    return replace(state, state_revision=state.state_revision + 1, morning=morning)

