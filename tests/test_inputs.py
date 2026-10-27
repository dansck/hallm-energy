"""Snapshot from Home Assistant states: freshness, off-peak, slot starts."""

from __future__ import annotations

from datetime import time, timedelta

import pytest

from custom_components.hallm_energy.config import EnergyConfig
from custom_components.hallm_energy.inputs import (
    RELAY_HORIZON,
    SCHEDULE_MAX_AGE,
    build_inputs,
    parse_hhmm,
    parse_schedule,
    schedule_nt,
)
from tests.test_lifecycle import COUNTER, T0

OPTIONS = {
    "soc_entity": "sensor.bms_soc",
    "counter_entity": COUNTER,
    "counter_epoch": "e1",
    "hdo_relay_entity": "binary_sensor.hdo_nt",
    "hdo_schedule_entity": "sensor.hdo_schedule",
    "grid_charge_entity": "switch.inv_grid_charge",
    "shadow_entity": "input_boolean.shadow",
    "feature_entity": "input_boolean.grid_recovery",
}
STARTS = (600, 700, 900, 1000, 1600, 1700)
for _i in range(1, 7):
    OPTIONS[f"slot{_i}_start_entity"] = f"number.inv_slot{_i}_start"
    OPTIONS[f"slot{_i}_capacity_entity"] = f"number.inv_slot{_i}_capacity"
    OPTIONS[f"slot{_i}_charge_enable_entity"] = f"switch.inv_slot{_i}_charge_enable"
CONFIG = EnergyConfig.from_options(OPTIONS)
NOW = T0 + timedelta(minutes=15)  # 22:15 UTC


def _schedule(now=NOW, *, tariff="NT", hours=6):
    return [
        {"start": (now - timedelta(hours=hours)).isoformat(), "end": now.isoformat(),
         "tariff": "VT"},
        {"start": now.isoformat(), "end": (now + timedelta(hours=hours)).isoformat(),
         "tariff": tariff},
    ]


def set_telemetry(hass, *, soc="19.0", raw="7204.0", relay="on", schedule=None,
                  last_update=None, starts=STARTS) -> None:
    states = hass.states
    states.async_set("sensor.bms_soc", soc)
    states.async_set(COUNTER, raw)
    states.async_set("binary_sensor.hdo_nt", relay)
    states.async_set("sensor.hdo_schedule", "27.10.2026", {
        "schedule": _schedule() if schedule is None else schedule,
        "last_update": (last_update or NOW - timedelta(hours=1)).isoformat(),
    })
    for i, start in enumerate(starts, start=1):
        states.async_set(f"number.inv_slot{i}_start", str(start))


@pytest.fixture
def at_now(freezer):
    freezer.move_to(NOW)
    return NOW


def test_parse_schedule_rejects_malformed_items():
    assert parse_schedule([]) is None
    assert parse_schedule("x") is None
    assert parse_schedule([{"start": "2026-10-27T00:00:00", "end": "2026-10-27T06:00:00",
                            "tariff": "NT"}]) is None  # naive times
    assert parse_schedule([{"start": NOW.isoformat(), "end": NOW.isoformat(),
                            "tariff": "NT"}]) is None  # empty interval
    assert parse_schedule([{"start": NOW.isoformat(),
                            "end": (NOW + timedelta(hours=1)).isoformat(),
                            "tariff": "low"}]) is None


def test_schedule_gap_or_contradiction_is_unknown():
    intervals = parse_schedule(_schedule())
    assert schedule_nt(intervals, NOW) is True
    assert schedule_nt(intervals, NOW - timedelta(minutes=1)) is False
    assert schedule_nt(intervals, NOW + timedelta(hours=7)) is None
    overlapping = parse_schedule(_schedule() + [
        {"start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat(),
         "tariff": "VT"}])
    assert schedule_nt(overlapping, NOW) is None


@pytest.mark.parametrize(("value", "expected"), [
    (600.0, time(6)), (1730.0, time(17, 30)), (0.0, time(0)),
    (660.0, None), (2400.0, None), (600.5, None), (-100.0, None), (None, None),
])
def test_parse_hhmm(value, expected):
    assert parse_hhmm(value) == expected


async def test_complete_fresh_inputs(hass, at_now):
    set_telemetry(hass)
    inputs = build_inputs(hass, CONFIG, NOW)
    snap = inputs.snapshot
    assert inputs.problems == ()
    assert snap.telemetry_valid and snap.soc_pct == 19.0
    assert snap.reading.available and snap.reading.raw_kwh == 7204.0
    assert snap.reading.counter_id == COUNTER and snap.reading_epoch == "e1"
    assert snap.slot_starts == tuple(time(s // 100) for s in STARTS)
    assert snap.is_nt(NOW) is True


async def test_long_unchanged_counter_is_still_available(hass, freezer):
    freezer.move_to(NOW - timedelta(hours=5))
    set_telemetry(hass)
    freezer.move_to(NOW)
    reading = build_inputs(hass, CONFIG, NOW).snapshot.reading
    assert reading.available
    assert reading.at == NOW - timedelta(hours=5)  # time of the last actual change


@pytest.mark.parametrize("raw", ["unavailable", "unknown", "-1", "nan", "x"])
async def test_unusable_counter_is_unavailable(hass, at_now, raw):
    set_telemetry(hass, raw=raw)
    inputs = build_inputs(hass, CONFIG, NOW)
    assert inputs.snapshot.reading.available is False
    assert "counter_unavailable" in inputs.problems


@pytest.mark.parametrize("soc", ["unavailable", "unknown", "101", "-2"])
async def test_unusable_soc_invalidates_telemetry(hass, at_now, soc):
    set_telemetry(hass, soc=soc)
    snap = build_inputs(hass, CONFIG, NOW).snapshot
    assert snap.soc_pct is None and snap.telemetry_valid is False


async def test_relay_off_while_schedule_says_nt_is_not_nt(hass, at_now):
    set_telemetry(hass, relay="off")
    inputs = build_inputs(hass, CONFIG, NOW)
    assert inputs.snapshot.is_nt(NOW) is False
    assert "hdo_relay_schedule_mismatch" in inputs.problems


async def test_relay_on_while_schedule_says_vt_is_not_nt(hass, at_now):
    set_telemetry(hass, schedule=_schedule(tariff="VT"))
    inputs = build_inputs(hass, CONFIG, NOW)
    assert inputs.snapshot.is_nt(NOW) is False
    assert "hdo_relay_schedule_mismatch" in inputs.problems


@pytest.mark.parametrize("relay", ["unavailable", "unknown"])
async def test_unknown_relay_is_not_nt(hass, at_now, relay):
    set_telemetry(hass, relay=relay)
    assert build_inputs(hass, CONFIG, NOW).snapshot.is_nt(NOW) is False


async def test_stale_schedule_is_not_nt(hass, at_now):
    set_telemetry(hass, last_update=NOW - SCHEDULE_MAX_AGE - timedelta(minutes=1))
    inputs = build_inputs(hass, CONFIG, NOW)
    assert inputs.snapshot.is_nt(NOW) is False
    assert "hdo_schedule_invalid" in inputs.problems


async def test_schedule_not_covering_now_is_not_nt(hass, at_now):
    set_telemetry(hass, schedule=_schedule(NOW - timedelta(hours=8), hours=1))
    inputs = build_inputs(hass, CONFIG, NOW)
    assert inputs.snapshot.is_nt(NOW) is False
    assert "hdo_schedule_not_covering_now" in inputs.problems


async def test_relay_does_not_vouch_for_other_times(hass, at_now):
    set_telemetry(hass)
    is_nt = build_inputs(hass, CONFIG, NOW).snapshot.is_nt
    assert is_nt(NOW + RELAY_HORIZON) is True
    assert is_nt(NOW + RELAY_HORIZON + timedelta(seconds=1)) is False


@pytest.mark.parametrize("starts", [
    (600, 700, 900, 1000, 1600, 1600),   # not strictly ascending
    (700, 600, 900, 1000, 1600, 1700),
    (600, 700, 960, 1000, 1600, 1700),   # 09:60
])
async def test_invalid_slot_starts_invalidate_telemetry(hass, at_now, starts):
    set_telemetry(hass, starts=starts)
    inputs = build_inputs(hass, CONFIG, NOW)
    assert inputs.snapshot.slot_starts is None
    assert inputs.snapshot.telemetry_valid is False
    assert "slot_starts_invalid" in inputs.problems


async def test_unavailable_slot_start_invalidates_telemetry(hass, at_now):
    set_telemetry(hass)
    hass.states.async_set("number.inv_slot3_start", "unavailable")
    assert build_inputs(hass, CONFIG, NOW).snapshot.telemetry_valid is False


# --- recovery inputs -------------------------------------------------------------------

from custom_components.hallm_energy.inputs import (  # noqa: E402
    offpeak_steps,
    pv_forecast,
    schedule_intervals,
)

PV = "sensor.pv_forecast"
PV_CONFIG = EnergyConfig.from_options({**OPTIONS, "pv_forecast_entity": PV, "pv_correction": 0.75})


def _pv_state(hass, *, start=None, watts=4000, days=("today", "tomorrow")):
    start = start or NOW.replace(hour=0, minute=0)
    attrs = {}
    for i, day in enumerate(days):
        base = start + timedelta(days=i)
        attrs[f"profile_{day}_timestamps"] = [
            (base + timedelta(minutes=15 * k)).isoformat() for k in range(96)]
        attrs[f"profile_{day}_w"] = [watts] * 96
    hass.states.async_set(PV, "0.0", attrs)


async def test_pv_forecast_applies_correction_exactly_once(hass):
    _pv_state(hass)
    pv = pv_forecast(hass, PV_CONFIG, NOW)
    assert pv(NOW) == pytest.approx(4.0 * 0.75)
    assert pv(NOW + timedelta(days=3)) == 0.0  # beyond the forecast nothing is credited


async def test_pv_forecast_unusable_when_not_covering_now_or_invalid(hass):
    _pv_state(hass, start=NOW.replace(hour=0, minute=0) + timedelta(days=3))
    assert pv_forecast(hass, PV_CONFIG, NOW) is None
    _pv_state(hass, watts=-5)
    assert pv_forecast(hass, PV_CONFIG, NOW) is None
    _pv_state(hass, days=("today",))
    assert pv_forecast(hass, PV_CONFIG, NOW) is None  # tomorrow missing = malformed
    assert pv_forecast(hass, CONFIG, NOW) is None  # no forecast entity configured


async def test_offpeak_steps_only_whole_nt_steps(hass, at_now):
    set_telemetry(hass)  # NT from NOW for 6 h, VT before
    steps = offpeak_steps(schedule_intervals(hass, CONFIG, NOW), NOW)
    assert steps[0] == NOW and len(steps) == 24
    assert all(s >= NOW for s in steps)
    half = NOW + timedelta(minutes=7)
    assert offpeak_steps(schedule_intervals(hass, CONFIG, half), half)[0] == NOW + timedelta(
        minutes=15)
    assert offpeak_steps(None, NOW) == ()


async def test_offpeak_step_cut_by_tariff_change_is_excluded(hass, at_now):
    end = NOW + timedelta(hours=1, minutes=7)  # NT ends inside the 23:15 step
    set_telemetry(hass, schedule=[
        {"start": (NOW - timedelta(hours=1)).isoformat(), "end": end.isoformat(), "tariff": "NT"},
        {"start": end.isoformat(), "end": (NOW + timedelta(hours=6)).isoformat(),
         "tariff": "VT"},
    ])
    steps = offpeak_steps(schedule_intervals(hass, CONFIG, NOW), NOW)
    assert steps == tuple(NOW + timedelta(minutes=15 * i) for i in range(4))
