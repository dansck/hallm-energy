"""Morning exception: `morning_dip` then `pv_refill` — one state
machine for the planner and the integration. Called every 15 minutes with the
persisted `MorningState`; it never calls `recovery_decision` itself, it only
says `handoff_to_recovery`.

Forecast freshness is judged by the time the forecast was RETRIEVED
(`forecast_retrieved_at`), not by the age of the weather model (contract 33).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from .grant import ceil5, dc_kwh_to_soc_pct, soc_to_dc_kwh
from .recovery import STEP_MINUTES, pv_refill_sim, time_to_reach

MorningPhase = Literal["none", "morning_dip", "pv_refill"]


@dataclass(frozen=True)
class MorningState:
    """State persisted between calls (every 15 minutes). `floor_state_pct` only
    has a meaning in the `pv_refill` phase."""

    phase: MorningPhase = "none"
    floor_state_pct: float | None = None


@dataclass(frozen=True)
class MorningDecision:
    phase: MorningPhase
    floor_pct: float
    next_state: MorningState
    handoff_to_recovery: bool
    reserve_restored_by: datetime | None = None


def _integrate_ac_kwh(
    fn: Callable[[datetime], float], t0: datetime, t1: datetime, step_minutes: int = STEP_MINUTES
) -> float:
    if t1 <= t0:
        return 0.0
    step = timedelta(minutes=step_minutes)
    total = 0.0
    t = t0
    while t < t1:
        step_end = min(t + step, t1)
        total += fn(t) * (step_end - t).total_seconds() / 3600
        t = step_end
    return total


def _refill_reaches_reserve_margin(
    *,
    soc_pct: float,
    t0: datetime,
    t_end: datetime,
    reserve_pct: float,
    c_use_kwh: float,
    eta_c: float,
    eta_d: float,
    e_err_morning_kwh: float,
    pv_surplus_ac_kw: Callable[[datetime], float],
) -> bool:
    """Shared by entering phase 1 and the running recalculation of phase 2: does the
    step-wise refill reach `E(reserve) + E_err_morning` by `t_end`?"""
    if t_end <= t0:
        return soc_pct >= reserve_pct
    sim = pv_refill_sim(
        e0_dc_kwh=soc_to_dc_kwh(soc_pct, c_use_kwh),
        t0=t0,
        t_end=t_end,
        mode="conservative",
        dispatch="refill",
        reserve_pct=reserve_pct,
        c_use_kwh=c_use_kwh,
        eta_c=eta_c,
        eta_d=eta_d,
        pv_surplus_ac_kw=pv_surplus_ac_kw,
        battery_covered_deficit_ac_kw=lambda _t: 0.0,
    )
    target = soc_to_dc_kwh(reserve_pct, c_use_kwh) + e_err_morning_kwh
    return time_to_reach(sim.trajectory, target) is not None


def _phase1_feasible(
    *,
    now: datetime,
    soc_now_pct: float,
    t_pv_start: datetime,
    t_pv_end: datetime,
    reserve_pct: float,
    dip_floor_pct: float,
    c_use_kwh: float,
    eta_c: float,
    eta_d: float,
    e_err_morning_kwh: float,
    forecast_retrieved_at: datetime,
    forecast_max_age_h: float,
    forecast_covers_window: bool,
    battery_covered_load_ac_kw: Callable[[datetime], float],
    pv_surplus_ac_kw: Callable[[datetime], float],
) -> bool:
    """Conditions ii-iv: forecast current and covering the window, the projected SOC at
    sunrise not below the dip floor, and the refill feasible. Condition i (window, gate,
    island, recovery) is the caller's: it differs between entering and recalculating."""
    forecast_stale = (now - forecast_retrieved_at) > timedelta(hours=forecast_max_age_h)
    if forecast_stale or not forecast_covers_window:
        return False
    e_batt_cov_ac = _integrate_ac_kwh(battery_covered_load_ac_kw, now, t_pv_start)
    soc_proj = dc_kwh_to_soc_pct(
        soc_to_dc_kwh(soc_now_pct, c_use_kwh) - e_batt_cov_ac / eta_d, c_use_kwh
    )
    if soc_proj < dip_floor_pct:
        return False
    return _refill_reaches_reserve_margin(
        soc_pct=soc_proj,
        t0=t_pv_start,
        t_end=t_pv_end,
        reserve_pct=reserve_pct,
        c_use_kwh=c_use_kwh,
        eta_c=eta_c,
        eta_d=eta_d,
        e_err_morning_kwh=e_err_morning_kwh,
        pv_surplus_ac_kw=pv_surplus_ac_kw,
    )


def morning_exception_decision(
    *,
    state: MorningState,
    now: datetime,
    soc_now_pct: float,
    t_pv_start: datetime,
    t_pv_end: datetime,
    reserve_pct: float,
    morning_dip_floor_pct: float,
    morning_dip_h: float,
    c_use_kwh: float,
    eta_c: float,
    eta_d: float,
    e_err_morning_kwh: float,
    forecast_retrieved_at: datetime,
    forecast_max_age_h: float,
    forecast_covers_window: bool,
    enable_morning_dip: bool,
    is_island: bool,
    entered_from_recovery: bool,
    battery_covered_load_ac_kw: Callable[[datetime], float],
    pv_surplus_ac_kw: Callable[[datetime], float],
) -> MorningDecision:
    """Called every 15 minutes. Never calls `recovery_decision` itself (that needs grant
    parameters that do not belong here): `handoff_to_recovery=True` tells the caller to
    call it in this very iteration."""
    none_state = MorningState("none")
    phase1_kwargs: dict[str, Any] = {
        "t_pv_start": t_pv_start,
        "t_pv_end": t_pv_end,
        "reserve_pct": reserve_pct,
        "dip_floor_pct": morning_dip_floor_pct,
        "c_use_kwh": c_use_kwh,
        "eta_c": eta_c,
        "eta_d": eta_d,
        "e_err_morning_kwh": e_err_morning_kwh,
        "forecast_retrieved_at": forecast_retrieved_at,
        "forecast_max_age_h": forecast_max_age_h,
        "forecast_covers_window": forecast_covers_window,
        "battery_covered_load_ac_kw": battery_covered_load_ac_kw,
        "pv_surplus_ac_kw": pv_surplus_ac_kw,
    }

    if state.phase == "pv_refill":
        if soc_now_pct >= reserve_pct:
            return MorningDecision("none", reserve_pct, none_state, False, reserve_restored_by=now)
        if now >= t_pv_end:
            return MorningDecision("none", reserve_pct, none_state, True)
        floor_now = min(reserve_pct, max(state.floor_state_pct or reserve_pct, ceil5(soc_now_pct)))
        if _refill_reaches_reserve_margin(
            soc_pct=soc_now_pct,
            t0=now,
            t_end=t_pv_end,
            reserve_pct=reserve_pct,
            c_use_kwh=c_use_kwh,
            eta_c=eta_c,
            eta_d=eta_d,
            e_err_morning_kwh=e_err_morning_kwh,
            pv_surplus_ac_kw=pv_surplus_ac_kw,
        ):
            next_state = MorningState("pv_refill", floor_now)
            return MorningDecision("pv_refill", floor_now, next_state, False)
        # worse forecast: hand over to recovery
        return MorningDecision("none", floor_now, none_state, True)

    if state.phase == "morning_dip":
        if now >= t_pv_start:
            floor_now = ceil5(soc_now_pct)
            next_state = MorningState("pv_refill", floor_now)
            return MorningDecision("pv_refill", floor_now, next_state, False)
        if _phase1_feasible(now=now, soc_now_pct=soc_now_pct, **phase1_kwargs):
            return MorningDecision("morning_dip", morning_dip_floor_pct, state, False)
        # forecast worsened during phase 1: it ends early, try phase 2 right away
        floor_now = ceil5(soc_now_pct)
        if _refill_reaches_reserve_margin(
            soc_pct=soc_now_pct,
            t0=now,
            t_end=t_pv_end,
            reserve_pct=reserve_pct,
            c_use_kwh=c_use_kwh,
            eta_c=eta_c,
            eta_d=eta_d,
            e_err_morning_kwh=e_err_morning_kwh,
            pv_surplus_ac_kw=pv_surplus_ac_kw,
        ):
            next_state = MorningState("pv_refill", floor_now)
            return MorningDecision("pv_refill", floor_now, next_state, False)
        return MorningDecision("none", floor_now, none_state, True)

    # state.phase == "none": try to enter phase 1
    cond_i = (
        enable_morning_dip
        and not is_island
        and not entered_from_recovery
        and (t_pv_start - timedelta(hours=morning_dip_h)) <= now <= t_pv_start
    )
    if cond_i and _phase1_feasible(now=now, soc_now_pct=soc_now_pct, **phase1_kwargs):
        return MorningDecision(
            "morning_dip", morning_dip_floor_pct, MorningState("morning_dip"), False
        )
    return MorningDecision("none", reserve_pct, none_state, False)
