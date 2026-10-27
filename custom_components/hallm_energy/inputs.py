"""Builds the executor's `Snapshot` from Home Assistant states. Anything
unknown, unavailable, stale or contradictory makes the affected input invalid —
never a reason to enable.

* Freshness: the SOC and the charge counter legitimately stay unchanged for a
  long time (the counter only moves while charging), so their timestamps prove
  nothing. The device integration must report them as unknown/unavailable
  when its link fails (e.g. ESPHome publishing NaN); that is what is checked.
* Off-peak (`is_nt`): the measured tariff relay AND a current tariff schedule
  must both say off-peak. The relay only describes "now", so `is_nt(t)` is
  false for any `t` farther than `RELAY_HORIZON` from the snapshot time.
* Slot starts are read from the inverter (`HHMM` numbers), never assumed.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .config import EnergyConfig
from .energy_core.grant import CounterReading
from .executor import Snapshot

SCHEDULE_MAX_AGE = timedelta(hours=36)
RELAY_HORIZON = timedelta(minutes=5)

Interval = tuple[datetime, datetime, bool]  # start, end, off-peak


@dataclass(frozen=True)
class Inputs:
    snapshot: Snapshot
    problems: tuple[str, ...]


def _usable(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _number(state: State | None) -> float | None:
    if not _usable(state):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def parse_schedule(raw: Any) -> tuple[Interval, ...] | None:
    """`[{start, end, tariff: "NT"|"VT"}]` with offset-aware ISO times;
    anything malformed → `None` (whole schedule unusable)."""
    if not isinstance(raw, list) or not raw:
        return None
    intervals = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        start, end = _aware(item.get("start")), _aware(item.get("end"))
        tariff = item.get("tariff")
        if start is None or end is None or end <= start or tariff not in ("NT", "VT"):
            return None
        intervals.append((start, end, tariff == "NT"))
    return tuple(intervals)


def schedule_nt(intervals: tuple[Interval, ...], at: datetime) -> bool | None:
    """Off-peak at `at` per the schedule; `None` if not covered or contradictory."""
    found = {nt for start, end, nt in intervals if start <= at < end}
    return found.pop() if len(found) == 1 else None


def parse_hhmm(value: float | None) -> time | None:
    if value is None or value != int(value) or value < 0:
        return None
    hours, minutes = divmod(int(value), 100)
    return time(hours, minutes) if hours < 24 and minutes < 60 else None


def build_inputs(hass: HomeAssistant, config: EnergyConfig, now: datetime) -> Inputs:
    problems: list[str] = []
    states = hass.states

    soc = _number(states.get(config.soc_entity))
    if soc is None or not 0 <= soc <= 100:
        problems.append("soc_unavailable")
        soc = None

    counter_state = states.get(config.counter_entity)
    raw = _number(counter_state)
    counter_ok = raw is not None and raw >= 0
    if not counter_ok:
        problems.append("counter_unavailable")
    reading = CounterReading(
        counter_id=config.counter_entity,
        raw_kwh=raw if counter_ok else 0.0,
        at=counter_state.last_changed if counter_ok else now,
        available=counter_ok,
    )

    starts = tuple(parse_hhmm(_number(states.get(e))) for e in config.slot_start_entities)
    slot_starts: tuple[time, ...] | None = None
    if None in starts or list(starts) != sorted(set(starts)):
        problems.append("slot_starts_invalid")
    else:
        slot_starts = starts  # type: ignore[assignment]

    is_nt = _is_nt(hass, config, now, problems)

    return Inputs(
        snapshot=Snapshot(
            now=now,
            local_time=dt_util.as_local(now).time().replace(tzinfo=None),
            soc_pct=soc,
            telemetry_valid=soc is not None and slot_starts is not None,
            reading=reading,
            reading_epoch=config.counter_epoch,
            is_nt=is_nt,
            slot_starts=slot_starts,
        ),
        problems=tuple(problems),
    )


def _is_nt(hass: HomeAssistant, config: EnergyConfig, now: datetime,
           problems: list[str]) -> Callable[[datetime], bool]:
    relay = hass.states.get(config.hdo_relay_entity)
    relay_nt = relay.state == "on" if _usable(relay) and relay.state in ("on", "off") else None
    if relay_nt is None:
        problems.append("hdo_relay_unavailable")

    schedule_state = hass.states.get(config.hdo_schedule_entity)
    intervals = None
    if _usable(schedule_state):
        updated = _aware(schedule_state.attributes.get("last_update"))
        if updated is not None and now - updated <= SCHEDULE_MAX_AGE:
            intervals = parse_schedule(schedule_state.attributes.get("schedule"))
    if intervals is None:
        problems.append("hdo_schedule_invalid")
    else:
        planned = schedule_nt(intervals, now)
        if planned is None:
            problems.append("hdo_schedule_not_covering_now")
        elif relay_nt is not None and planned != relay_nt:
            problems.append("hdo_relay_schedule_mismatch")

    def is_nt(at: datetime) -> bool:
        return (
            relay_nt is True
            and intervals is not None
            and abs(at - now) <= RELAY_HORIZON
            and schedule_nt(intervals, at) is True
        )

    return is_nt
