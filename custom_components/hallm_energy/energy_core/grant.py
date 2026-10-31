"""Grid-charging grant accounting — pure functions, no I/O.

A grant is the only authorization to charge the home battery from the grid:
a stable identity (purpose, target, DC energy budget, battery charge counter
and its checkpoint), a time window, and measured usage. The main stop is the
target SOC (the inverter itself stops charging there); the DC budget times a
safety factor is only a slow backup limit against faults.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

GrantPurpose = Literal["recovery", "econ"]

# Backup limit only: the target SOC is the main stop.
DEFAULT_GRANT_BUDGET_FACTOR = 1.25

# The battery charge counter has a 0.1 kWh step — tolerance for telling "the
# counter went down" apart from rounding or a not-yet-updated value.
COUNTER_STEP_TOLERANCE_KWH = 0.1

# Conservative ceiling of physically possible DC charge power; used only to
# detect physically impossible counter jumps, not as an operating limit.
DEFAULT_MAX_CHARGE_RATE_KW = 10.0


def ceil5(value: float) -> float:
    """Round up to the nearest multiple of 5 (register quantization)."""
    return math.ceil(round(value, 9) / 5) * 5


def soc_to_dc_kwh(soc_pct: float, c_use_kwh: float) -> float:
    """Stored DC energy at a given SOC and usable capacity."""
    return soc_pct / 100 * c_use_kwh


def dc_kwh_to_soc_pct(energy_kwh: float, c_use_kwh: float) -> float:
    return energy_kwh / c_use_kwh * 100


def recovery_target_valid(*, reserve_pct: float, recovery_target_pct: float) -> bool:
    """`ceil5(target) >= ceil5(reserve) + 5` (reserve 30 / target 30 is invalid)."""
    return ceil5(recovery_target_pct) >= ceil5(reserve_pct) + 5


def effective_recovery_target_pct(*, reserve_pct: float, recovery_target_pct: float) -> float:
    """Effective (quantized) recovery target used for the budget — 27 % is effectively 30 %.

    Range: `ceil5(reserve)+5 … reserve+20`. A value outside is clipped to the
    nearest valid bound; validity of the input parameter itself is checked by
    `recovery_target_valid` before it is written, this only computes the
    effective value for the budget.
    """
    lo = ceil5(reserve_pct) + 5
    hi = reserve_pct + 20
    return min(max(ceil5(recovery_target_pct), lo), hi)


def grant_budget_dc_kwh(
    *, c_use_kwh: float, target_pct: float, soc_at_creation_pct: float
) -> float:
    """`budget_dc = max(0, E(target_eff) - E(SOC_at_creation))`."""
    target_kwh = soc_to_dc_kwh(target_pct, c_use_kwh)
    at_creation_kwh = soc_to_dc_kwh(soc_at_creation_pct, c_use_kwh)
    return max(0.0, target_kwh - at_creation_kwh)


@dataclass(frozen=True)
class GrantIdentity:
    """Facts recorded when the grant is created — never change afterwards: a new
    plan, epoch or restart must not move them."""

    id: str
    purpose: GrantPurpose
    created_at: datetime
    target_pct: float
    budget_dc_kwh: float
    counter_id: str
    checkpoint_dc_kwh: float


@dataclass(frozen=True)
class GrantWindow:
    """Allocated time frame — may be extended within the SAME episode/ID when the
    window expires before the target is reached; that does not change identity
    or budget.

    Off-peak (low tariff) membership is NOT stored as a list of steps: it is
    checked live for `now` via the caller's `is_nt` predicate against the
    current tariff schedule, not against a selection frozen at creation.

    The interval is half-open `[start, end)`: a grant created with
    `end == start == now` must never start, which a closed interval would
    allow at exactly the creation instant."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class Grant:
    identity: GrantIdentity
    window: GrantWindow


def create_recovery_grant(
    *,
    grant_id: str,
    now: datetime,
    soc_now_pct: float,
    reserve_pct: float,
    recovery_target_pct: float,
    c_use_kwh: float,
    counter_id: str,
    checkpoint_dc_kwh: float,
    window_end: datetime,
) -> Grant:
    """Create a recovery grant. `checkpoint_dc_kwh` must be the counter value read
    BEFORE charging is enabled (the caller owns the write ordering)."""
    target_eff = effective_recovery_target_pct(
        reserve_pct=reserve_pct, recovery_target_pct=recovery_target_pct
    )
    budget = grant_budget_dc_kwh(
        c_use_kwh=c_use_kwh, target_pct=target_eff, soc_at_creation_pct=soc_now_pct
    )
    identity = GrantIdentity(
        id=grant_id,
        purpose="recovery",
        created_at=now,
        target_pct=target_eff,
        budget_dc_kwh=budget,
        counter_id=counter_id,
        checkpoint_dc_kwh=checkpoint_dc_kwh,
    )
    window = GrantWindow(start=now, end=window_end)
    return Grant(identity=identity, window=window)


def extend_grant_window(grant: Grant, *, now: datetime, new_end: datetime) -> Grant:
    """Window expired without reaching the target: same ID/identity/budget, only a
    longer frame (the new end must not be in the past)."""
    if new_end <= now:
        raise ValueError("new grant window end must not be in the past")
    return Grant(
        identity=grant.identity,
        window=GrantWindow(start=grant.window.start, end=new_end),
    )


@dataclass(frozen=True)
class CounterReading:
    """One reading of the battery's total DC charge counter (kWh DC, gross,
    charging only — discharging does not move it)."""

    counter_id: str
    raw_kwh: float
    at: datetime
    available: bool = True


@dataclass(frozen=True)
class GrantUsageUpdate:
    used_dc_kwh: float
    at: datetime | None
    trusted: bool
    reason: str | None = None


def update_grant_used(
    grant: Grant,
    reading: CounterReading,
    prior_used_dc_kwh: float,
    prior_at: datetime | None = None,
    *,
    max_charge_rate_kw: float = DEFAULT_MAX_CHARGE_RATE_KW,
) -> GrantUsageUpdate:
    """`used_dc = raw_counter_now - checkpoint` — all gross charging, discharge is
    not subtracted. Unavailable counter, changed identity, decrease or a
    physically impossible jump → usage is NOT advanced (`trusted=False`); an
    undocumented remainder of the budget is not released.

    `prior_at` is the time of the counter's LAST ACTUAL CHANGE, not of the last
    successful poll: the inverter updates this register only about every
    10 minutes while it is polled more often. Advancing `prior_at` on every poll
    without a value change would shorten the plausibility window to the time
    since the last poll, and a normal batch increment after a longer pause
    would look like an impossible jump. So `prior_at` is only advanced when the
    counter actually changed (beyond the step tolerance)."""
    if not reading.available:
        return GrantUsageUpdate(prior_used_dc_kwh, prior_at, False, "counter_unavailable")
    if reading.counter_id != grant.identity.counter_id:
        return GrantUsageUpdate(prior_used_dc_kwh, prior_at, False, "counter_identity_changed")
    raw_used = reading.raw_kwh - grant.identity.checkpoint_dc_kwh
    if raw_used < prior_used_dc_kwh - COUNTER_STEP_TOLERANCE_KWH:
        return GrantUsageUpdate(prior_used_dc_kwh, prior_at, False, "counter_decreased")
    if raw_used <= prior_used_dc_kwh + COUNTER_STEP_TOLERANCE_KWH:
        return GrantUsageUpdate(prior_used_dc_kwh, prior_at, True)  # counter unchanged
    if prior_at is not None and reading.at > prior_at:
        elapsed_h = (reading.at - prior_at).total_seconds() / 3600
        plausible_max = (
            prior_used_dc_kwh + max_charge_rate_kw * elapsed_h + COUNTER_STEP_TOLERANCE_KWH
        )
        if raw_used > plausible_max:
            return GrantUsageUpdate(prior_used_dc_kwh, prior_at, False, "counter_jump_implausible")
    return GrantUsageUpdate(max(raw_used, prior_used_dc_kwh), reading.at, True)


def grant_remaining_dc_kwh(grant: Grant, used_dc_kwh: float) -> float:
    return max(0.0, grant.identity.budget_dc_kwh - used_dc_kwh)


def grant_budget_exhausted(
    grant: Grant, used_dc_kwh: float, budget_factor: float = DEFAULT_GRANT_BUDGET_FACTOR
) -> bool:
    """Backup limit — NOT an exact purchase limit: `used >= budget * factor`."""
    return used_dc_kwh >= grant.identity.budget_dc_kwh * budget_factor


def grant_target_reached(grant: Grant, soc_now_pct: float) -> bool:
    """Main stop: the inverter stops charging at `capacity` = target itself; this
    only confirms it."""
    return soc_now_pct >= grant.identity.target_pct


def estimate_grant_ac_kwh(
    used_dc_kwh: float, eta_c_estimate: float | None
) -> tuple[float | None, Literal["estimated", "unknown"]]:
    """AC report only (does not affect DC grant authorization): missing
    efficiency = `unknown`, never a reason to refuse recovery."""
    if eta_c_estimate is None or eta_c_estimate <= 0:
        return None, "unknown"
    return used_dc_kwh / eta_c_estimate, "estimated"


def floor_after_recovery(
    *, recovery_target_pct: float, reserve_pct: float, cloudy_hold: bool
) -> float:
    """The single transition after a grant closes, identical everywhere
    (executor, fallback, planner): `cloudy_hold` keeps the floor at the
    recovery target, otherwise back to the reserve."""
    return recovery_target_pct if cloudy_hold else reserve_pct


@dataclass(frozen=True)
class GrantDecision:
    charge_enable: bool
    close: bool
    reason: str | None


def evaluate_grant(
    grant: Grant,
    used_dc_kwh: float,
    *,
    now: datetime,
    soc_now_pct: float,
    telemetry_valid: bool,
    is_nt: Callable[[datetime], bool],
    counter_trusted: bool = True,
    counter_distrust_reason: str | None = None,
    budget_factor: float = DEFAULT_GRANT_BUDGET_FACTOR,
    hold_reason: str | None = None,
) -> GrantDecision:
    """Whether the grant currently allows charging, with a reason.

    Order of checks (`counter_trusted` must never be conflated with general
    `telemetry_valid`): first general safety telemetry (SOC/power/tariff —
    without it `soc_now_pct` cannot be trusted either; pause, not close);
    then the main stop (target SOC) — applies EVEN with an untrusted counter,
    because the SOC stop does not depend on the DC counter; then trust in the
    counter itself — without it the budget backup cannot be evaluated safely,
    so only pause (NOT close) keeping `used_dc_kwh`/checkpoint; then the budget
    backup; then an external hold (`hold_reason`: something outside the grant, such as the phase
    guard, forbids charging for now: a pause, never a close, and charging resumes when it ends);
    then scheduling (window and live off-peak membership of `now`)."""
    if not telemetry_valid:
        return GrantDecision(False, False, "telemetry_stale")
    if grant_target_reached(grant, soc_now_pct):
        return GrantDecision(False, True, "target_reached")
    if not counter_trusted:
        return GrantDecision(False, False, counter_distrust_reason or "counter_untrusted")
    if grant_budget_exhausted(grant, used_dc_kwh, budget_factor):
        return GrantDecision(False, True, "budget_exhausted")
    if hold_reason is not None:
        return GrantDecision(False, False, hold_reason)
    if not (grant.window.start <= now < grant.window.end):  # half-open interval
        return GrantDecision(False, False, "outside_window")
    if not is_nt(now):
        return GrantDecision(False, False, "outside_mask")
    return GrantDecision(True, False, None)


@dataclass(frozen=True)
class EnergyGrantState:
    """Active grant (`grant=None` = none) plus its measured usage."""

    grant: Grant | None
    used_dc_kwh: float = 0.0
    used_at: datetime | None = None


@dataclass(frozen=True)
class GrantClosure:
    grant_id: str
    reason: str
    at: datetime
    budget_dc_kwh: float
    final_used_dc_kwh: float


def close_grant(
    state: EnergyGrantState, *, at: datetime, reason: str
) -> tuple[EnergyGrantState, GrantClosure]:
    """Closing is one-way: returns an empty state (ready for a future grant) plus
    a closure record. Callers persisting state must store both together."""
    if state.grant is None:
        raise ValueError("no active grant to close")
    closure = GrantClosure(
        grant_id=state.grant.identity.id,
        reason=reason,
        at=at,
        budget_dc_kwh=state.grant.identity.budget_dc_kwh,
        final_used_dc_kwh=state.used_dc_kwh,
    )
    return EnergyGrantState(grant=None), closure


def advance_grant(
    state: EnergyGrantState,
    *,
    now: datetime,
    soc_now_pct: float,
    reading: CounterReading,
    telemetry_valid: bool,
    is_nt: Callable[[datetime], bool],
    budget_factor: float = DEFAULT_GRANT_BUDGET_FACTOR,
    max_charge_rate_kw: float = DEFAULT_MAX_CHARGE_RATE_KW,
) -> tuple[EnergyGrantState, GrantDecision, GrantClosure | None]:
    """One step of an existing grant — usage + evaluation + optional closing; the
    same path for the normal cycle and for resuming after a restart."""
    if state.grant is None:
        return state, GrantDecision(False, False, "no_grant"), None
    update = update_grant_used(
        state.grant,
        reading,
        state.used_dc_kwh,
        state.used_at,
        max_charge_rate_kw=max_charge_rate_kw,
    )
    working = EnergyGrantState(state.grant, update.used_dc_kwh, update.at)
    decision = evaluate_grant(
        state.grant,
        update.used_dc_kwh,
        now=now,
        soc_now_pct=soc_now_pct,
        telemetry_valid=telemetry_valid,
        is_nt=is_nt,
        counter_trusted=update.trusted,
        counter_distrust_reason=update.reason,
        budget_factor=budget_factor,
    )
    if decision.close:
        closed_state, closure = close_grant(working, at=now, reason=decision.reason or "closed")
        return closed_state, decision, closure
    return working, decision, None
