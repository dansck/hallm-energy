"""EV charging state, report only: reads the car and wallbox entities and the requests calendar,
runs the pure state decision and keeps the result in memory. It never writes anything.

Not decided here (reported as unavailable): charging from surplus and battery assist, which
depend on inverter behaviour that has not been verified; the minimum dwell time of a state."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .config import EnergyConfig
from .energy_core.ev import (
    EvRequest,
    deadline_feasibility,
    effective_ev_target_pct,
    ev_state,
    mobility_floor_needed,
    next_request,
)
from .energy_core.ev_plan import MIN_POWER_KW, WallboxFacts, plan_ev_intents

_LOGGER = logging.getLogger(__name__)

CALENDAR_REFRESH = timedelta(minutes=5)
CALENDAR_HORIZON = timedelta(days=14)
CALENDAR_TIMEOUT_S = 10.0
SOC_UNUSABLE_AFTER = timedelta(hours=24)
CLOCK_SKEW = timedelta(minutes=5)  # a reading from the future beyond this is not believed
ASSIST_WINDOW_H = 3.0


def _event_start(raw: Any) -> datetime | None:
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date")
    if not isinstance(raw, str):
        return None
    try:
        if len(raw) == 10:
            return dt_util.as_utc(dt_util.start_of_local_day(date.fromisoformat(raw)))
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt_util.as_utc(parsed if parsed.tzinfo else parsed.replace(
        tzinfo=dt_util.get_default_time_zone()))


class EvShadow:
    def __init__(self, hass: HomeAssistant, config: EnergyConfig) -> None:
        self.hass, self.config = hass, config
        self._events: list[tuple[datetime, str]] = []
        self._fetched_at: datetime | None = None
        self._calendar_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.ev_soc_entity and self.config.ev_connected_entity)

    async def async_refresh_calendar(self, now: datetime) -> None:
        """At most every few minutes. A failed read keeps the last good events and is reported."""
        entity = self.config.ev_calendar_entity
        if entity is None or (self._fetched_at is not None
                              and now - self._fetched_at < CALENDAR_REFRESH):
            return
        self._fetched_at = now
        try:
            async with asyncio.timeout(CALENDAR_TIMEOUT_S):
                response = await self.hass.services.async_call(
                    "calendar", "get_events",
                    {"entity_id": entity, "start_date_time": now.isoformat(),
                     "duration": {"days": CALENDAR_HORIZON.days}},
                    blocking=True, return_response=True)
            raw = (response or {}).get(entity, {}).get("events", [])
            events = []
            for item in raw:
                start = _event_start(item.get("start"))
                if start is not None:
                    events.append((start, str(item.get("summary") or "")))
            self._events, self._calendar_error = events, None
        except Exception as exc:  # the report must never stop anything else
            self._calendar_error = repr(exc)
            _LOGGER.warning("EV calendar not read: %s", exc)

    def _number(self, entity: str | None) -> tuple[float | None, datetime | None]:
        state = self.hass.states.get(entity) if entity else None
        if state is None:
            return None, None
        seen = max(state.last_updated, getattr(state, "last_reported", state.last_updated))
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None, seen
        return (value, seen) if math.isfinite(value) else (None, seen)

    def _wallbox_facts(self) -> WallboxFacts:
        cfg, states = self.config, self.hass.states

        def flag(entity: str | None) -> bool | None:
            state = states.get(entity) if entity else None
            if state is None or state.state not in ("on", "off"):
                return None
            return state.state == "on"

        return WallboxFacts(
            paused=flag(cfg.ev_wallbox_pause_entity),
            intensity_a=self._number(cfg.ev_wallbox_intensity_entity)[0],
            max_intensity_a=self._number(cfg.ev_wallbox_max_intensity_entity)[0],
            dynamic_on=flag(cfg.ev_wallbox_dynamic_entity))

    def evaluate(self, now: datetime, guard: dict | None, grid_present: bool | None,
                 force: bool = False) -> dict[str, Any] | None:
        """`None` when the EV report is not configured."""
        cfg = self.config
        if not self.enabled:
            return None
        report: dict[str, Any] = {
            "state": None, "reason": None, "calendar_error": self._calendar_error,
            "not_evaluated": ["pv_charge", "battery_assist"],
        }
        soc, seen = self._number(cfg.ev_soc_entity)
        if soc is None:
            report["reason"] = "soc_unavailable"
            return report
        if not 0.0 <= soc <= 100.0:
            report["reason"] = "soc_invalid"
            return report
        if cfg.ev_soc_updated_entity:  # the car's own reading time, not the Home Assistant report
            stamp = self.hass.states.get(cfg.ev_soc_updated_entity)
            seen = dt_util.parse_datetime(stamp.state) if stamp is not None else None
            if seen is None:
                report["reason"] = "soc_time_unknown"
                return report
            seen = dt_util.as_utc(seen)
        age = now - seen
        report["soc_pct"], report["soc_age_min"] = soc, int(age.total_seconds() // 60)
        if age < -CLOCK_SKEW:
            report["reason"] = "soc_time_invalid"
            return report
        if age > SOC_UNUSABLE_AFTER:
            report["reason"] = "soc_stale"
            return report

        plugged = self.hass.states.get(cfg.ev_connected_entity)
        home = True
        if cfg.ev_tracker_entity:
            tracker = self.hass.states.get(cfg.ev_tracker_entity)
            home = tracker is not None and tracker.state == "home"
        connected = plugged is not None and plugged.state == "on" and home
        report["connected_at_home"] = connected

        request: EvRequest | None = next_request(self._events, now)
        target = effective_ev_target_pct(
            now=now,
            explicit_target_pct=None if request is None else request.target_pct,
            deadline_at=None if request is None else request.deadline_at,
            calendar_target_pct=None,
            default_target_pct=cfg.ev_default_target_pct)
        curtailed = bool(guard and guard.get("ev_paused")) or grid_present is False
        decision = ev_state(
            connected_at_home=connected,
            effective_target_pct=target,
            soc_pct=soc,
            curtailed=curtailed,
            deadline_active=request is not None,
            deadline_at=None if request is None else request.deadline_at,
            now=now,
            ev_assist_h=ASSIST_WINDOW_H,
            battery_assist_eligible=False,
            mobility_floor_needed=mobility_floor_needed(
                soc_pct=soc, mobility_min_pct=cfg.ev_mobility_min_pct,
                trip_pct_per_day=cfg.ev_daily_trip_pct),
            pv_charge_eligible=False,
            deferred_eligible=True,
            force_charge=force)
        report["state"] = decision.state
        report["effective_target_pct"] = decision.effective_target_pct
        report["force_requested"] = force
        if decision.state == "force_charge":
            cap = guard["ev_current_cap_a"] if guard else cfg.guard_ev_cap_a
            report |= {"tariff_ignored": True, "power": "max", "max_current_a": cap}
        if cfg.ev_wallbox_pause_entity:
            facts = self._wallbox_facts()
            if guard is not None:
                cap_a = guard["ev_current_cap_a"]
            else:  # a configured guard that has not reported leaves the ceiling unknown
                cap_a = None if cfg.phase_current_entities else cfg.guard_ev_cap_a
            intents, withheld = plan_ev_intents(decision.state, wallbox=facts, cap_a=cap_a)
            report["wallbox"] = {
                "paused": facts.paused, "intensity_a": facts.intensity_a,
                "max_intensity_a": facts.max_intensity_a, "dynamic_on": facts.dynamic_on,
                "min_power_kw": round(MIN_POWER_KW, 2)}
            report["intents"] = [
                {"target": i.target, "value": i.value, "reason": i.reason, "phase": i.phase}
                for i in intents]
            report["intents_withheld"] = withheld
        if request is not None:
            energy, hours, feasible = deadline_feasibility(
                now=now, deadline_at=request.deadline_at, soc_pct=soc,
                target_pct=target, ac_kwh_per_pct=cfg.ev_ac_kwh_per_pct,
                max_power_kw=cfg.ev_max_power_kw)
            report["request"] = {
                "title": request.title, "deadline_at": request.deadline_at.isoformat(),
                "target_pct": target, "energy_needed_kwh": round(energy, 1),
                "hours_needed_at_max": round(hours, 1), "feasible": feasible}
        return report
