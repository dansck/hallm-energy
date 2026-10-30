"""What the wallbox should be told, given the EV state: a plan, never a write.

The wallbox cannot charge below `MIN_CURRENT_A` (6 A, about 4.1 kW on three phases). Its own
dynamic modulation follows the surplus but keeps at least that minimum, so it does not stop when
the surplus is smaller: it draws the rest from the grid or the battery. A plan that wants "surplus
only" therefore has to pause the wallbox itself, and a plan that wants full power has to switch the
dynamic modulation off. Only intents that differ from the present wallbox state are returned.

Charging from the grid is allowed by the tariff gate: routine charging (mobility floor) only in
off-peak; a deadline in off-peak, and in peak only when the off-peak time left before the deadline
is not enough and the owner allows it. An unknown tariff never opens the gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

MIN_CURRENT_A = 6.0
PHASES = 3
VOLTAGE_V = 230.0
MIN_POWER_KW = PHASES * VOLTAGE_V * MIN_CURRENT_A / 1000.0  # 4.14 kW

IntentTarget = Literal["pause", "intensity", "max_intensity", "dynamic"]
IntentPhase = Literal["protect", "prepare", "enable"]
_UNPLANNED = ("battery_assist", "pv_charge")
_GRID_STATES = ("mobility_floor", "routine", "deadline")


@dataclass(frozen=True)
class TariffFacts:
    """`nt_now`: off-peak right now (`None` = unknown). `nt_hours_to_deadline`: off-peak hours
    between now and the deadline, `covered`: whether the schedule reaches the deadline (when it
    does not, the figure is only a lower bound)."""

    nt_now: bool | None = None
    nt_hours_to_deadline: float | None = None
    covered: bool = False


def grid_charge_gate(
    state: str,
    *,
    tariff: TariffFacts | None,
    hours_needed: float | None,
    allow_peak_for_deadline: bool,
    defer_topup: bool = False,
) -> tuple[bool | None, str]:
    """`(allowed, reason)` for charging from the grid in this state; `allowed` is `None` when
    the tariff is not known well enough to say."""
    if defer_topup:
        return False, "top-up above the hold level waits until shortly before departure"
    if tariff is None or tariff.nt_now is None:
        return None, "tariff unknown"
    if tariff.nt_now:
        return True, "off-peak"
    if state in ("mobility_floor", "routine"):
        return False, "peak: routine charging waits for off-peak"
    if not allow_peak_for_deadline:
        return False, "peak: the owner does not allow peak charging for a deadline"
    if hours_needed is None or tariff.nt_hours_to_deadline is None:
        return None, "deadline energy or off-peak time unknown"
    if tariff.nt_hours_to_deadline >= hours_needed:
        return False, "peak: the off-peak time before the deadline is enough"
    if not tariff.covered:
        return None, "the schedule does not reach the deadline"
    return True, "peak: the off-peak time before the deadline is not enough"


@dataclass(frozen=True)
class WallboxFacts:
    paused: bool | None = None
    intensity_a: float | None = None
    max_intensity_a: float | None = None
    dynamic_on: bool | None = None


@dataclass(frozen=True)
class EvIntent:
    """One step of a plan. The tuple order is the order to carry it out in: `protect` steps
    (pausing) first, then `prepare` (mode, ceiling, current), then `enable` (un-pausing) last."""

    target: IntentTarget
    value: bool | float
    reason: str
    phase: IntentPhase = "prepare"


def _pause(wallbox: WallboxFacts, reason: str) -> tuple[tuple[EvIntent, ...], str | None]:
    if wallbox.paused is True:
        return (), None
    return (EvIntent("pause", True, reason, "protect"),), None  # unknown counts as not paused


def _full_power(
    wallbox: WallboxFacts, limit_a: int
) -> tuple[tuple[EvIntent, ...], str | None]:
    """Full power up to the ceiling in force. Everything that decides how much the wallbox may
    draw has to be known before it is un-paused: an unknown field never lets it through."""
    if None in (wallbox.paused, wallbox.intensity_a, wallbox.max_intensity_a, wallbox.dynamic_on):
        return (), "wallbox state incomplete"
    steps: list[EvIntent] = []
    if wallbox.dynamic_on:
        steps.append(EvIntent(
            "dynamic", False, "full power: dynamic modulation would hold the minimum"))
    ceiling = EvIntent("max_intensity", float(limit_a), "ceiling in force")
    current = EvIntent("intensity", float(limit_a), "full power")
    # lowering: the current first, then the ceiling; raising: the ceiling first
    ordered = [current, ceiling] if limit_a < wallbox.max_intensity_a else [ceiling, current]
    steps += [i for i in ordered
              if (wallbox.max_intensity_a if i.target == "max_intensity"
                  else wallbox.intensity_a) != i.value]
    if wallbox.paused:
        steps.append(EvIntent("pause", False, "forced charge", "enable"))
    return tuple(steps), None


def _charge(wallbox: WallboxFacts, cap_a: float | None):
    if cap_a is None or not math.isfinite(cap_a) or cap_a < 0:
        return (), "current ceiling unavailable"
    limit_a = math.floor(cap_a)
    if limit_a < MIN_CURRENT_A:
        return _pause(wallbox, "ceiling below the wallbox minimum: no charging power available")
    return _full_power(wallbox, limit_a)


def plan_ev_intents(
    state: str,
    *,
    wallbox: WallboxFacts,
    cap_a: float | None,
    tariff: TariffFacts | None = None,
    hours_needed: float | None = None,
    allow_peak_for_deadline: bool = True,
    defer_topup: bool = False,
) -> tuple[tuple[EvIntent, ...], str | None]:
    """`(intents, withheld_reason)`. `cap_a` is the current ceiling in force (the phase guard's
    cap), `None` when it is not known. Charging is discrete: off, or between the wallbox minimum
    and the ceiling. A ceiling below the minimum means no charging power at all, so the plan is a
    pause; it is never raised to the minimum. An unknown or invalid ceiling never permits
    charging. A state whose plan depends on things not evaluated here (surplus, battery) gives
    no intents and says why."""
    if state == "away":
        return (), None
    if state == "curtailed":
        return _pause(wallbox, "protection or grid outage")
    if state in ("idle", "deferred"):
        return _pause(wallbox, f"{state}: no charging wanted")
    if state in _UNPLANNED:
        return (), "surplus and battery are not evaluated yet"
    if state == "force_charge":
        return _charge(wallbox, cap_a)
    if state in _GRID_STATES:
        allowed, reason = grid_charge_gate(
            state, tariff=tariff, hours_needed=hours_needed,
            allow_peak_for_deadline=allow_peak_for_deadline, defer_topup=defer_topup)
        if allowed is None:
            return (), reason
        if not allowed:
            return _pause(wallbox, reason)
        return _charge(wallbox, cap_a)
    return (), f"unknown state {state}"
