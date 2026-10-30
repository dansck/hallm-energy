"""What the wallbox should be told, given the EV state: a plan, never a write.

The wallbox cannot charge below `MIN_CURRENT_A` (6 A, about 4.1 kW on three phases). Its own
dynamic modulation follows the surplus but keeps at least that minimum, so it does not stop when
the surplus is smaller: it draws the rest from the grid or the battery. A plan that wants "surplus
only" therefore has to pause the wallbox itself, and a plan that wants full power has to switch the
dynamic modulation off. Only intents that differ from the present wallbox state are returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MIN_CURRENT_A = 6.0
PHASES = 3
VOLTAGE_V = 230.0
MIN_POWER_KW = PHASES * VOLTAGE_V * MIN_CURRENT_A / 1000.0  # 4.14 kW

IntentTarget = Literal["pause", "intensity", "max_intensity", "dynamic"]
_UNPLANNED = ("mobility_floor", "deadline", "battery_assist", "pv_charge")


@dataclass(frozen=True)
class WallboxFacts:
    paused: bool | None = None
    intensity_a: float | None = None
    max_intensity_a: float | None = None
    dynamic_on: bool | None = None


@dataclass(frozen=True)
class EvIntent:
    target: IntentTarget
    value: bool | float
    reason: str


def plan_ev_intents(
    state: str,
    *,
    wallbox: WallboxFacts,
    cap_a: float,
) -> tuple[tuple[EvIntent, ...], str | None]:
    """`(intents, withheld_reason)`. `cap_a` is the current-limit ceiling in force (the phase
    guard's cap, at most the installation's normal value). A state whose plan depends on things
    not evaluated here (tariff window, surplus, battery) gives no intents and says why."""
    cap = max(MIN_CURRENT_A, cap_a)
    wanted: list[EvIntent] = []
    if state == "away":
        return (), None
    if state == "curtailed":
        wanted.append(EvIntent("pause", True, "protection or grid outage"))
    elif state == "force_charge":
        wanted += [
            EvIntent("pause", False, "forced charge"),
            EvIntent("dynamic", False, "full power: dynamic modulation would hold the minimum"),
            EvIntent("max_intensity", cap, "ceiling in force"),
            EvIntent("intensity", cap, "full power"),
        ]
    elif state in ("idle", "deferred"):
        wanted.append(EvIntent("pause", True, f"{state}: no charging wanted"))
    elif state in _UNPLANNED:
        return (), "tariff window and surplus are not evaluated yet"
    else:
        return (), f"unknown state {state}"

    current = {
        "pause": wallbox.paused,
        "dynamic": wallbox.dynamic_on,
        "max_intensity": wallbox.max_intensity_a,
        "intensity": wallbox.intensity_a,
    }
    differing = tuple(
        i for i in wanted if current[i.target] is not None and current[i.target] != i.value
    )
    return differing, None
