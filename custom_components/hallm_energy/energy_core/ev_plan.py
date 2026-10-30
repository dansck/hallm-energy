"""What the wallbox should be told, given the EV state: a plan, never a write.

The wallbox cannot charge below `MIN_CURRENT_A` (6 A, about 4.1 kW on three phases). Its own
dynamic modulation follows the surplus but keeps at least that minimum, so it does not stop when
the surplus is smaller: it draws the rest from the grid or the battery. A plan that wants "surplus
only" therefore has to pause the wallbox itself, and a plan that wants full power has to switch the
dynamic modulation off. Only intents that differ from the present wallbox state are returned.
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
_UNPLANNED = ("mobility_floor", "deadline", "battery_assist", "pv_charge")


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


def plan_ev_intents(
    state: str,
    *,
    wallbox: WallboxFacts,
    cap_a: float | None,
) -> tuple[tuple[EvIntent, ...], str | None]:
    """`(intents, withheld_reason)`. `cap_a` is the current ceiling in force (the phase guard's
    cap), `None` when it is not known. Charging is discrete: off, or between the wallbox minimum
    and the ceiling. A ceiling below the minimum means no charging power at all, so the plan is a
    pause; it is never raised to the minimum. An unknown or invalid ceiling never permits
    charging. A state whose plan depends on things not evaluated here (tariff window, surplus,
    battery) gives no intents and says why."""
    if state == "away":
        return (), None
    if state == "curtailed":
        return _pause(wallbox, "protection or grid outage")
    if state in ("idle", "deferred"):
        return _pause(wallbox, f"{state}: no charging wanted")
    if state in _UNPLANNED:
        return (), "tariff window and surplus are not evaluated yet"
    if state != "force_charge":
        return (), f"unknown state {state}"
    if cap_a is None or not math.isfinite(cap_a) or cap_a < 0:
        return (), "current ceiling unavailable"
    limit_a = math.floor(cap_a)
    if limit_a < MIN_CURRENT_A:
        return _pause(wallbox, "ceiling below the wallbox minimum: no charging power available")
    return _full_power(wallbox, limit_a)
