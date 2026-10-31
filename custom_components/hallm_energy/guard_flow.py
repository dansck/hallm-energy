"""Per-phase guard, report only: reads the phase sensors, runs the pure guard and keeps its
state in memory. It never writes anything; what it reports is what a live guard would do."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .config import EnergyConfig
from .energy_core.phase_guard import STALE_S, PhaseGuardState, branch_currents, phase_guard


@dataclass(frozen=True)
class PhaseReading:
    currents_a: tuple[float | None, ...]
    powers_w: tuple[float | None, ...]
    measured_at: datetime | None  # the oldest of all sensors; `None` when one has no state


def _number(hass: HomeAssistant, entity: str) -> tuple[float | None, datetime | None]:
    state = hass.states.get(entity)
    if state is None:
        return None, None
    seen = max(state.last_updated, getattr(state, "last_reported", state.last_updated))
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None, seen
    return value, seen


def read_phases(hass: HomeAssistant, config: EnergyConfig) -> PhaseReading:
    currents, powers, stamps = [], [], []
    for entity, out in ([(e, currents) for e in config.phase_current_entities]
                        + [(e, powers) for e in config.phase_power_entities]):
        value, seen = _number(hass, entity)
        out.append(value)
        stamps.append(seen)
    measured = None if any(s is None for s in stamps) or not stamps else min(stamps)
    return PhaseReading(tuple(currents), tuple(powers), measured)


class GuardShadow:
    """State between ticks. A restart begins from the unrestricted state, which is right for
    a report: the timers start again from the next sample."""

    def __init__(self, config: EnergyConfig) -> None:
        self._state = PhaseGuardState(config.guard_ev_cap_a, False, True, config.guard_export_cap_w)

    def evaluate(self, hass: HomeAssistant, config: EnergyConfig, now: datetime,
                 ev_charging: bool = True) -> dict[str, Any] | None:
        """`None` when the guard is not configured."""
        if not config.phase_current_entities:
            return None
        reading = read_phases(hass, config)
        worst_import, worst_export = branch_currents(reading.currents_a, reading.powers_w)
        decision = phase_guard(
            self._state,
            now=now,
            measured_at=reading.measured_at or datetime.min.replace(tzinfo=now.tzinfo),
            max_import_phase_a=worst_import,
            max_export_phase_a=worst_export,
            default_export_cap_w=config.guard_export_cap_w,
            default_ev_current_cap_a=config.guard_ev_cap_a,
            ev_charging=ev_charging,
        )
        self._state = decision.next_state
        stale = reading.measured_at is None or (
            now - reading.measured_at).total_seconds() > STALE_S
        return {
            "stale": stale,
            "import_a": worst_import,
            "export_a": worst_export,
            "ev_current_cap_a": decision.ev_current_cap_a,
            "ev_paused": decision.ev_paused,
            "grid_charge_allowed": decision.grid_charge_enabled,
            "export_cap_w": decision.export_cap_w,
            "notify": decision.notify,
            "measured_at": None if reading.measured_at is None else reading.measured_at.isoformat(),
            "restricted": (decision.ev_paused or not decision.grid_charge_enabled
                           or decision.ev_current_cap_a < config.guard_ev_cap_a
                           or decision.export_cap_w < config.guard_export_cap_w),
        }
