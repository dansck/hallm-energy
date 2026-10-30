"""EV charging state, report only: entity reading, calendar requests, decisions, no writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import SupportsResponse

from custom_components.hallm_energy.config import ConfigInvalidError, EnergyConfig
from custom_components.hallm_energy.ev_flow import EvShadow
from tests.test_controller import _controller, _fresh_state
from tests.test_executor import FakeInverter
from tests.test_inputs import CONFIG, NOW, OPTIONS, set_telemetry

SOC, PLUG, HOME, CAL = ("sensor.car_soc", "binary_sensor.car_plug", "device_tracker.car",
                        "calendar.requests")


def _ev_config(**over):
    return replace(CONFIG, ev_soc_entity=SOC, ev_connected_entity=PLUG,
                   ev_tracker_entity=HOME, ev_calendar_entity=CAL, **over)


def _car(hass, soc="50", plugged="on", home="home"):
    hass.states.async_set(SOC, soc)
    hass.states.async_set(PLUG, plugged)
    hass.states.async_set(HOME, home)


def _calendar(hass, events, calls=None):
    async def get_events(call):
        if calls is not None:
            calls.append(dict(call.data))
        return {CAL: {"events": events}}

    hass.services.async_register("calendar", "get_events", get_events,
                                 supports_response=SupportsResponse.ONLY)


async def _state(hass, config=None, guard=None, grid_present=True):
    shadow = EvShadow(hass, config or _ev_config())
    await shadow.async_refresh_calendar(NOW)
    return shadow, shadow.evaluate(NOW, guard, grid_present)


# --- configuration ------------------------------------------------------------------------


def test_the_report_is_off_without_car_entities():
    assert EnergyConfig.from_options(OPTIONS).ev_soc_entity is None
    assert EvShadow(None, CONFIG).evaluate(NOW, None, True) is None


def test_soc_and_connected_sensors_must_come_together_and_be_the_right_kind():
    with pytest.raises(ConfigInvalidError):
        EnergyConfig.from_options({**OPTIONS, "ev_soc_entity": SOC})
    with pytest.raises(ConfigInvalidError):
        EnergyConfig.from_options({**OPTIONS, "ev_soc_entity": "switch.x",
                                   "ev_connected_entity": PLUG})
    cfg = EnergyConfig.from_options({**OPTIONS, "ev_soc_entity": SOC,
                                     "ev_connected_entity": PLUG, "ev_calendar_entity": CAL})
    assert cfg.ev_calendar_entity == CAL and cfg.ev_daily_trip_pct == 10.0


# --- decisions ----------------------------------------------------------------------------


async def test_a_car_that_is_not_plugged_in_is_away(hass):
    _car(hass, plugged="off")
    _calendar(hass, [])
    _, report = await _state(hass)
    assert report["state"] == "away" and report["connected_at_home"] is False


async def test_a_plugged_car_away_from_home_is_away(hass):
    _car(hass, home="not_home")
    _calendar(hass, [])
    _, report = await _state(hass)
    assert report["state"] == "away"


async def test_an_unavailable_or_stale_soc_gives_no_decision(hass):
    _car(hass, soc="unavailable")
    _calendar(hass, [])
    _, report = await _state(hass)
    assert report["state"] is None and report["reason"] == "soc_unavailable"
    _car(hass, soc="50")
    shadow = EvShadow(hass, _ev_config())
    later = hass.states.get(SOC).last_updated + timedelta(hours=25)
    report = shadow.evaluate(later, None, True)
    assert report["state"] is None and report["reason"] == "soc_stale"


async def test_low_soc_without_a_request_is_the_mobility_floor(hass):
    _car(hass, soc="33")
    _calendar(hass, [])
    _, report = await _state(hass)
    assert report["state"] == "mobility_floor" and report["effective_target_pct"] == 80.0


async def test_a_comfortable_soc_below_the_target_is_deferred_and_at_target_idle(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _, report = await _state(hass)
    assert report["state"] == "deferred"
    _car(hass, soc="81")
    _, report = await _state(hass)
    assert report["state"] == "idle"


async def test_a_calendar_request_makes_a_deadline_with_its_target_and_feasibility(hass):
    _car(hass, soc="50")
    calls: list[dict] = []
    start = (NOW + timedelta(hours=20)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 90"},
                     {"start": start, "end": start, "summary": "Zubař"}], calls)
    _, report = await _state(hass)
    assert report["state"] == "deadline" and report["effective_target_pct"] == 90.0
    request = report["request"]
    assert request["title"] == "EV 90" and request["target_pct"] == 90.0
    assert request["energy_needed_kwh"] > 30 and request["feasible"] is True
    assert calls and calls[0]["entity_id"] == CAL


async def test_a_request_that_cannot_be_met_in_time_is_reported_infeasible(hass):
    _car(hass, soc="20")
    start = (NOW + timedelta(hours=2)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV plno"}])
    _, report = await _state(hass)
    assert report["state"] == "deadline" and report["request"]["feasible"] is False


async def test_a_reached_request_target_is_idle(hass):
    _car(hass, soc="85")
    start = (NOW + timedelta(hours=20)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 80"}])
    _, report = await _state(hass)
    assert report["state"] == "idle"


async def test_an_all_day_event_is_a_deadline_at_the_start_of_that_day(hass):
    _car(hass, soc="50")
    day = (NOW + timedelta(days=2)).date().isoformat()
    _calendar(hass, [{"start": day, "end": day, "summary": "EV 80"}])
    _, report = await _state(hass)
    assert report["state"] == "deadline" and report["request"]["deadline_at"].startswith(day[:8])


async def test_a_paused_ev_or_a_missing_grid_is_curtailed(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _, report = await _state(hass, guard={"ev_paused": True})
    assert report["state"] == "curtailed"
    _, report = await _state(hass, grid_present=False)
    assert report["state"] == "curtailed"


async def test_a_failing_calendar_keeps_the_last_events_and_is_reported(hass):
    _car(hass, soc="50")
    start = (NOW + timedelta(hours=20)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 90"}])
    shadow, report = await _state(hass)
    assert report["state"] == "deadline"

    async def boom(call):
        raise RuntimeError("calendar down")

    hass.services.async_register("calendar", "get_events", boom,
                                 supports_response=SupportsResponse.ONLY)
    await shadow.async_refresh_calendar(NOW + timedelta(minutes=6))
    report = shadow.evaluate(NOW + timedelta(minutes=6), None, True)
    assert report["state"] == "deadline" and "calendar down" in report["calendar_error"]


async def test_the_calendar_is_not_asked_again_within_the_refresh_interval(hass):
    _car(hass)
    calls: list[dict] = []
    _calendar(hass, [], calls)
    shadow = EvShadow(hass, _ev_config())
    await shadow.async_refresh_calendar(NOW)
    await shadow.async_refresh_calendar(NOW + timedelta(minutes=2))
    assert len(calls) == 1
    await shadow.async_refresh_calendar(NOW + timedelta(minutes=6))
    assert len(calls) == 2


# --- in the loop --------------------------------------------------------------------------


async def test_the_tick_reports_the_ev_state_and_writes_no_wallbox_entity(storage_hass, freezer):
    freezer.move_to(NOW)
    FakeInverter(storage_hass)
    set_telemetry(storage_hass)
    controller = await _controller(storage_hass, _fresh_state(), live=True)
    controller.config = _ev_config()
    controller.ev = EvShadow(storage_hass, controller.config)
    _car(storage_hass, soc="33")
    _calendar(storage_hass, [])
    heard: list[int] = []
    controller.add_listener(lambda: heard.append(1))
    status = await controller.async_tick()
    assert controller.ev_report["state"] == "mobility_floor"
    assert not [w for w in status.writes if "wallbox" in w.op.entity_id]
    assert heard
