"""EV charging state: one state per moment, chosen by a fixed priority order.

Pure functions only. The state is a decision, never a write: the caller decides what to do with
it. Conditions that depend on live inverter behaviour (battery assist, charging from surplus)
arrive as ready-made eligibility flags, so this module never claims they were verified.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

EvState = Literal[
    "away",
    "idle",
    "deferred",
    "pv_charge",
    "mobility_floor",
    "deadline",
    "battery_assist",
    "curtailed",
]

REQUEST_MIN_TARGET_PCT = 20.0
REQUEST_MAX_TARGET_PCT = 100.0
_FULL_WORDS = ("plno", "plne", "full", "max")
_REQUEST_RE = re.compile(r"^\s*ev(?:\s+(?P<arg>\S+?))?\s*%?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class EvRequest:
    """A charge request: be at `target_pct` (None = the default target) by `deadline_at`."""

    deadline_at: datetime
    target_pct: float | None
    title: str


@dataclass(frozen=True)
class EvDecision:
    state: EvState
    effective_target_pct: float


def parse_request_title(title: str) -> tuple[bool, float | None]:
    """`(is_request, target)` from an event title: `EV 80`, `EV 80 %`, `EV plno` (= 100),
    `EV` alone (default target, `None`). Anything else is not a request. A number outside
    20-100 makes the title not a request: nothing is guessed."""
    match = _REQUEST_RE.match(title or "")
    if match is None:
        return False, None
    arg = match.group("arg")
    if arg is None:
        return True, None
    if arg.lower() in _FULL_WORDS:
        return True, REQUEST_MAX_TARGET_PCT
    try:
        value = float(arg.replace(",", ".").rstrip("%"))
    except ValueError:
        return False, None
    if not REQUEST_MIN_TARGET_PCT <= value <= REQUEST_MAX_TARGET_PCT:
        return False, None
    return True, value


def next_request(events: Iterable[tuple[datetime, str]], now: datetime) -> EvRequest | None:
    """The request with the nearest deadline still in the future. `events` are
    `(start, title)`; the start of an event is the deadline."""
    best: EvRequest | None = None
    for start, title in events:
        is_request, target = parse_request_title(title)
        if not is_request or start <= now:
            continue
        if best is None or start < best.deadline_at:
            best = EvRequest(start, target, title.strip())
    return best


def effective_ev_target_pct(
    *,
    now: datetime,
    explicit_target_pct: float | None,
    deadline_at: datetime | None,
    calendar_target_pct: float | None,
    default_target_pct: float,
) -> float:
    """The target of a valid request (a future deadline with a set target, or a calendar
    target), otherwise the default. "Done" is judged against this, not always against 80 %."""
    if deadline_at is not None and deadline_at > now and explicit_target_pct is not None:
        return explicit_target_pct
    if calendar_target_pct is not None:
        return calendar_target_pct
    return default_target_pct


def mobility_floor_needed(
    *, soc_pct: float, mobility_min_pct: float, trip_pct_per_day: float, days: float = 1.0
) -> bool:
    """The car must keep the protective minimum plus the usual trips until the next
    opportunity to charge (`days` ahead). Below that, routine charging may not wait."""
    return soc_pct < mobility_min_pct + trip_pct_per_day * days


def deadline_feasibility(
    *,
    now: datetime,
    deadline_at: datetime,
    soc_pct: float,
    target_pct: float,
    battery_kwh: float,
    charge_efficiency: float,
    max_power_kw: float,
) -> tuple[float, float, bool]:
    """`(energy_needed_ac_kwh, hours_needed_at_max_power, feasible)` for reaching the target at
    the deadline when charging at full power from now on. Zero when the target is reached."""
    missing_pct = max(0.0, target_pct - soc_pct)
    energy_kwh = missing_pct / 100.0 * battery_kwh / charge_efficiency
    hours_needed = energy_kwh / max_power_kw if max_power_kw > 0 else float("inf")
    hours_left = max(0.0, (deadline_at - now).total_seconds() / 3600.0)
    return energy_kwh, hours_needed, hours_needed <= hours_left


def ev_state(
    *,
    connected_at_home: bool,
    effective_target_pct: float,
    soc_pct: float,
    curtailed: bool,
    deadline_active: bool,
    deadline_at: datetime | None,
    now: datetime,
    ev_assist_h: float,
    battery_assist_eligible: bool,
    mobility_floor_needed: bool,
    pv_charge_eligible: bool,
    deferred_eligible: bool,
) -> EvDecision:
    """Priority order: curtailed, then deadline / battery assist, then mobility floor, then
    charging from surplus, then deferred, then idle; away when the car is not connected at home.

    A deadline needs an unmet target, which this function checks itself rather than trusting
    `deadline_active`: a finished request must not hold battery assist forever. Battery assist
    also needs the deadline to be in the future and within `ev_assist_h`: an expired deadline
    never justifies discharging the house battery. A reached target blocks every charging state
    regardless of the eligibility flags. The minimum dwell time of a state is the caller's
    business (hysteresis on top of this decision)."""
    if not connected_at_home:
        return EvDecision("away", effective_target_pct)
    if curtailed:
        return EvDecision("curtailed", effective_target_pct)
    target_reached = soc_pct >= effective_target_pct
    if deadline_active and not target_reached:
        assist_window = (
            battery_assist_eligible
            and deadline_at is not None
            and now < deadline_at <= now + timedelta(hours=ev_assist_h)
        )
        if assist_window:
            return EvDecision("battery_assist", effective_target_pct)
        return EvDecision("deadline", effective_target_pct)
    if mobility_floor_needed:
        return EvDecision("mobility_floor", effective_target_pct)
    if target_reached:
        return EvDecision("idle", effective_target_pct)
    if pv_charge_eligible:
        return EvDecision("pv_charge", effective_target_pct)
    if deferred_eligible:
        return EvDecision("deferred", effective_target_pct)
    return EvDecision("deferred", effective_target_pct)
