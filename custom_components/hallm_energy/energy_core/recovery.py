"""Step simulation and the recovery decision: wait for PV, or a grid grant.

`pv_refill_sim` is the single simulation used for the evening target,
recovery, the morning exception and PV refill; `recovery_decision` uses it to
choose `wait_pv` (PV restores the reserve before the anchored deadline — buy
nothing) or `grid_recovery` (create a grant for the target).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from .grant import (
    Grant,
    ceil5,
    create_recovery_grant,
    dc_kwh_to_soc_pct,
    effective_recovery_target_pct,
    grant_budget_dc_kwh,
    soc_to_dc_kwh,
)

STEP_MINUTES = 15

PvRefillMode = Literal["conservative", "optimistic"]
PvRefillDispatch = Literal["plan", "wait_pv", "refill"]


@dataclass(frozen=True)
class PvRefillPoint:
    t: datetime
    e_dc_kwh: float
    floor_pct: float


@dataclass(frozen=True)
class PvRefillResult:
    mode: PvRefillMode
    dispatch: PvRefillDispatch
    trajectory: tuple[PvRefillPoint, ...]
    clipped_charge_dc_kwh: float
    clipped_discharge_dc_kwh: float


def _refill_floor_pct(
    dispatch: PvRefillDispatch, reserve_pct: float, max_soc_so_far_pct: float
) -> float:
    """`plan`/`wait_pv` have a constant floor (the reserve); `refill` has a
    state floor that only rises: `min(reserve, ceil5(max SOC since entry))`."""
    if dispatch == "refill":
        return min(reserve_pct, ceil5(max_soc_so_far_pct))
    return reserve_pct


def pv_refill_sim(
    *,
    e0_dc_kwh: float,
    t0: datetime,
    t_end: datetime,
    mode: PvRefillMode,
    dispatch: PvRefillDispatch,
    reserve_pct: float,
    c_use_kwh: float,
    eta_c: float,
    eta_d: float,
    pv_surplus_ac_kw: Callable[[datetime], float],
    battery_covered_deficit_ac_kw: Callable[[datetime], float],
    charge_max_dc_kw: float | Callable[[datetime], float] = math.inf,
    discharge_max_dc_kw: float | Callable[[datetime], float] = math.inf,
    step_minutes: int = STEP_MINUTES,
) -> PvRefillResult:
    """Integrates in 15-minute steps with power and capacity limits.
    `pv_surplus_ac_kw` and `battery_covered_deficit_ac_kw` must already be
    NETTED inputs (production minus house consumption) — gross production is
    not a criterion. In each step the battery either charges or discharges,
    never both: a positive surplus charges (up to the power/capacity limit), a
    zero/negative one lets the covered deficit discharge down to the floor,
    never below it."""
    if t_end <= t0:
        raise ValueError("t_end must be after t0")
    charge_limit = charge_max_dc_kw if callable(charge_max_dc_kw) else (lambda _t: charge_max_dc_kw)
    discharge_limit = (
        discharge_max_dc_kw if callable(discharge_max_dc_kw) else (lambda _t: discharge_max_dc_kw)
    )
    step = timedelta(minutes=step_minutes)

    e = e0_dc_kwh
    t = t0
    max_soc_so_far = dc_kwh_to_soc_pct(e0_dc_kwh, c_use_kwh)
    initial_floor = _refill_floor_pct(dispatch, reserve_pct, max_soc_so_far)
    points = [PvRefillPoint(t0, e0_dc_kwh, initial_floor)]
    total_charge = 0.0
    total_discharge = 0.0

    while t < t_end:
        step_end = min(t + step, t_end)
        dt_h = (step_end - t).total_seconds() / 3600
        floor_pct = _refill_floor_pct(dispatch, reserve_pct, max_soc_so_far)
        floor_kwh = soc_to_dc_kwh(floor_pct, c_use_kwh)
        surplus_ac = pv_surplus_ac_kw(t)
        if surplus_ac > 0:
            charge_dc = min(
                surplus_ac * eta_c, charge_limit(t), max(0.0, c_use_kwh - e) / dt_h
            )
            discharge_dc = 0.0
        else:
            charge_dc = 0.0
            need_batt_ac = battery_covered_deficit_ac_kw(t)
            discharge_dc = min(
                need_batt_ac / eta_d, discharge_limit(t), max(0.0, e - floor_kwh) / dt_h
            )
        e += (charge_dc - discharge_dc) * dt_h
        total_charge += charge_dc * dt_h
        total_discharge += discharge_dc * dt_h
        t = step_end
        max_soc_so_far = max(max_soc_so_far, dc_kwh_to_soc_pct(e, c_use_kwh))
        points.append(PvRefillPoint(t, e, floor_pct))

    return PvRefillResult(mode, dispatch, tuple(points), total_charge, total_discharge)


def time_to_reach(trajectory: Sequence[PvRefillPoint], target_dc_kwh: float) -> datetime | None:
    """First time the trajectory reaches the target — linear interpolation
    inside the step where it is crossed."""
    if not trajectory:
        return None
    if trajectory[0].e_dc_kwh >= target_dc_kwh:
        return trajectory[0].t
    for prev, cur in zip(trajectory, trajectory[1:], strict=False):
        if cur.e_dc_kwh >= target_dc_kwh:
            if cur.e_dc_kwh == prev.e_dc_kwh:
                return cur.t
            fraction = (target_dc_kwh - prev.e_dc_kwh) / (cur.e_dc_kwh - prev.e_dc_kwh)
            return prev.t + (cur.t - prev.t) * fraction
    return None


@dataclass(frozen=True)
class RecoveryDecision:
    state: Literal["wait_pv", "grid_recovery"]
    wait_until: datetime
    grant: Grant | None
    target_pct: float
    floor_pct: float


def recovery_decision(
    *,
    grant_id: str,
    now: datetime,
    soc_now_pct: float,
    reserve_pct: float,
    recovery_target_pct: float,
    recovery_pv_wait_h: float,
    recovery_wait_until: datetime | None,
    c_use_kwh: float,
    eta_c: float,
    eta_d: float,
    e_err_morning_kwh: float,
    pv_surplus_ac_kw: Callable[[datetime], float],
    counter_id: str,
    checkpoint_dc_kwh: float,
    candidate_nt_steps: Sequence[datetime] = (),
    assumed_charge_rate_dc_kw: float = 1.0,
) -> RecoveryDecision:
    """One function for the planner and the local fallback. Called only below
    the reserve and outside the morning exception (the caller checks that).
    `recovery_wait_until` is anchored on first entry (`None`) and must then be
    passed unchanged for every later recalculation of the same episode; once
    it has passed without the reserve restored, the decision is
    `grid_recovery` (the deadline itself never moves).

    `candidate_nt_steps` are future off-peak steps, already filtered against
    the tariff schedule by the caller; they only set the window end (end of
    the last step needed). Off-peak membership inside the window is checked
    live by `evaluate_grant`; too few steps simply leave the window at `now`
    and the grant does not start until its window is extended."""
    if soc_now_pct >= reserve_pct:
        raise ValueError("recovery_decision is only called below the reserve")
    if recovery_wait_until is not None:
        wait_until = recovery_wait_until
    else:
        wait_until = now + timedelta(hours=recovery_pv_wait_h)

    if wait_until > now:  # after the anchored deadline without restoration: grid
        sim = pv_refill_sim(
            e0_dc_kwh=soc_to_dc_kwh(soc_now_pct, c_use_kwh),
            t0=now,
            t_end=wait_until,
            mode="conservative",
            dispatch="wait_pv",
            reserve_pct=reserve_pct,
            c_use_kwh=c_use_kwh,
            eta_c=eta_c,
            eta_d=eta_d,
            pv_surplus_ac_kw=pv_surplus_ac_kw,
            # recovery runs with the battery covering the backup output only — no discharge
            battery_covered_deficit_ac_kw=lambda _t: 0.0,
        )
        target_wait_dc = soc_to_dc_kwh(reserve_pct, c_use_kwh) + e_err_morning_kwh
        if time_to_reach(sim.trajectory, target_wait_dc) is not None:
            return RecoveryDecision("wait_pv", wait_until, None, reserve_pct, reserve_pct)

    target_eff = effective_recovery_target_pct(
        reserve_pct=reserve_pct, recovery_target_pct=recovery_target_pct
    )
    budget = grant_budget_dc_kwh(
        c_use_kwh=c_use_kwh, target_pct=target_eff, soc_at_creation_pct=soc_now_pct
    )
    step_h = STEP_MINUTES / 60
    steps_needed = math.ceil(budget / (assumed_charge_rate_dc_kw * step_h)) if budget > 0 else 0
    selected = tuple(candidate_nt_steps[:steps_needed])
    window_end = selected[-1] + timedelta(minutes=STEP_MINUTES) if selected else now
    grant = create_recovery_grant(
        grant_id=grant_id,
        now=now,
        soc_now_pct=soc_now_pct,
        reserve_pct=reserve_pct,
        recovery_target_pct=recovery_target_pct,
        c_use_kwh=c_use_kwh,
        counter_id=counter_id,
        checkpoint_dc_kwh=checkpoint_dc_kwh,
        window_end=window_end,
    )
    return RecoveryDecision("grid_recovery", wait_until, grant, target_eff, target_eff)
