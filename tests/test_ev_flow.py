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


@pytest.fixture(autouse=True)
def _clock(freezer):
    freezer.move_to(NOW)


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


# --- "must charge" ---------------------------------------------------------------------------


async def test_the_force_wish_makes_a_forced_charge_at_full_power_regardless_of_tariff(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    shadow = EvShadow(hass, _ev_config())
    await shadow.async_refresh_calendar(NOW)
    assert shadow.evaluate(NOW, None, True)["state"] == "deferred"
    report = shadow.evaluate(NOW, None, True, force=True)
    assert report["state"] == "force_charge" and report["force_requested"] is True
    assert report["tariff_ignored"] is True and report["power"] == "max"
    assert report["max_current_a"] == 16.0


async def test_the_forced_charge_current_follows_the_guard_cap(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    shadow = EvShadow(hass, _ev_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, {"ev_paused": False, "ev_current_cap_a": 12.0}, True, force=True)
    assert report["state"] == "force_charge" and report["max_current_a"] == 12.0
    report = shadow.evaluate(NOW, {"ev_paused": True, "ev_current_cap_a": 6.0}, True, force=True)
    assert report["state"] == "curtailed"


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_the_switch_exists_only_with_the_car_configured_and_drives_the_report(
        storage_hass, mqtt_mock):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.hallm_energy.const import DOMAIN

    hass = storage_hass
    hass.states.async_set("input_boolean.shadow", "on")
    hass.states.async_set("input_boolean.grid_recovery", "on")
    FakeInverter(hass)
    set_telemetry(hass, soc="50.0")
    _car(hass, soc="60")
    _calendar(hass, [])
    entry = MockConfigEntry(domain=DOMAIN, options={
        **OPTIONS, "ev_soc_entity": SOC, "ev_connected_entity": PLUG})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    switch = next(s for s in hass.states.async_all("switch")
                  if s.entity_id.endswith("ev_must_charge"))
    assert switch.state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch.entity_id},
                                   blocking=True)
    controller = entry.runtime_data.controller
    assert controller.ev_force is True
    assert controller.ev_report["state"] == "force_charge"
    assert hass.states.get(switch.entity_id).state == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch.entity_id},
                                   blocking=True)
    assert controller.ev_force is False and controller.ev_report["state"] == "deferred"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_there_is_no_switch_without_the_car(storage_hass, mqtt_mock):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.hallm_energy.const import DOMAIN

    hass = storage_hass
    hass.states.async_set("input_boolean.shadow", "on")
    hass.states.async_set("input_boolean.grid_recovery", "on")
    FakeInverter(hass)
    set_telemetry(hass, soc="50.0")
    entry = MockConfigEntry(domain=DOMAIN, options=dict(OPTIONS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    names = [s.entity_id for s in hass.states.async_all("switch")]
    assert not [n for n in names if n.endswith("ev_must_charge")]
    assert await hass.config_entries.async_unload(entry.entry_id)


# --- the wallbox plan ------------------------------------------------------------------------

PAUSE, INTENSITY, MAXI, DYNAMIC = ("switch.wb_pause", "number.wb_intensity", "number.wb_max",
                                   "switch.wb_dynamic")


def _wallbox(hass, *, paused="off", intensity="6", maxi="16", dynamic="off"):
    hass.states.async_set(PAUSE, paused)
    hass.states.async_set(INTENSITY, intensity)
    hass.states.async_set(MAXI, maxi)
    hass.states.async_set(DYNAMIC, dynamic)


def _wallbox_config():
    return _ev_config(ev_wallbox_pause_entity=PAUSE, ev_wallbox_intensity_entity=INTENSITY,
                      ev_wallbox_max_intensity_entity=MAXI, ev_wallbox_dynamic_entity=DYNAMIC)


def _intents(report):
    return {(i["target"], i["value"]) for i in report["intents"]}


async def test_without_wallbox_entities_the_report_has_no_plan(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _, report = await _state(hass)
    assert "intents" not in report and "wallbox" not in report


async def test_a_deferred_car_with_a_running_wallbox_would_be_paused(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="off")
    _, report = await _state(hass, config=_wallbox_config())
    assert report["state"] == "deferred" and _intents(report) == {("pause", True)}
    assert report["wallbox"]["min_power_kw"] == 4.14 and report["intents_withheld"] is None


async def test_a_forced_charge_would_unpause_drop_dynamic_modulation_and_go_full_power(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="on", intensity="6", dynamic="on")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, force=True)
    assert report["state"] == "force_charge"
    assert _intents(report) == {("pause", False), ("dynamic", False), ("intensity", 16.0)}


async def test_a_state_that_needs_the_tariff_is_withheld_not_guessed(hass):
    _car(hass, soc="33")
    _calendar(hass, [])
    _wallbox(hass)
    _, report = await _state(hass, config=_wallbox_config())
    assert report["state"] == "mobility_floor" and report["intents"] == []
    assert "not evaluated" in report["intents_withheld"]


async def test_the_report_changes_no_wallbox_entity(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="on", intensity="6", dynamic="on")
    before = {e: hass.states.get(e).state for e in (PAUSE, INTENSITY, MAXI, DYNAMIC)}
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    shadow.evaluate(NOW, None, True, force=True)
    assert {e: hass.states.get(e).state for e in before} == before


def test_the_wallbox_entities_are_checked_for_their_kind():
    with pytest.raises(ConfigInvalidError):
        EnergyConfig.from_options({**OPTIONS, "ev_soc_entity": SOC, "ev_connected_entity": PLUG,
                                   "ev_wallbox_pause_entity": "number.x"})
    cfg = EnergyConfig.from_options({**OPTIONS, "ev_soc_entity": SOC, "ev_connected_entity": PLUG,
                                     "ev_wallbox_pause_entity": PAUSE,
                                     "ev_wallbox_intensity_entity": INTENSITY})
    assert cfg.ev_wallbox_pause_entity == PAUSE and cfg.ev_wallbox_dynamic_entity is None


# --- invalid and untrustworthy readings (review 40) --------------------------------------------


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-5", "100.5", "101"])
async def test_an_invalid_soc_never_authorises_a_forced_charge(hass, raw):
    _car(hass, soc=raw)
    _calendar(hass, [])
    shadow = EvShadow(hass, _ev_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, force=True)
    assert report["state"] is None and report["reason"] in ("soc_invalid", "soc_unavailable")


@pytest.mark.parametrize(("raw", "state"), [("0", "force_charge"), ("100", "idle")])
async def test_the_limits_of_the_valid_soc_range_are_kept(hass, raw, state):
    _car(hass, soc=raw)
    _calendar(hass, [])
    shadow = EvShadow(hass, _ev_config())
    await shadow.async_refresh_calendar(NOW)
    assert shadow.evaluate(NOW, None, True, force=True)["state"] == state


UPDATED = "sensor.car_updated"


def _updated_config():
    return _ev_config(ev_soc_updated_entity=UPDATED)


async def test_the_age_of_the_soc_is_the_cars_own_reading_time_not_the_ha_report(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    hass.states.async_set(UPDATED, (NOW - timedelta(hours=30)).isoformat())
    shadow = EvShadow(hass, _updated_config())
    report = shadow.evaluate(NOW, None, True)
    assert report["state"] is None and report["reason"] == "soc_stale"
    assert report["soc_age_min"] == 30 * 60
    hass.states.async_set(UPDATED, (NOW - timedelta(minutes=30)).isoformat())
    report = shadow.evaluate(NOW, None, True)
    assert report["state"] == "deferred" and report["soc_age_min"] == 30


@pytest.mark.parametrize(("value", "reason"), [
    ("unavailable", "soc_time_unknown"), ("not a time", "soc_time_unknown"),
])
async def test_an_unusable_reading_time_gives_no_decision(hass, value, reason):
    _car(hass, soc="60")
    hass.states.async_set(UPDATED, value)
    report = EvShadow(hass, _updated_config()).evaluate(NOW, None, True, force=True)
    assert report["state"] is None and report["reason"] == reason


async def test_a_reading_time_from_the_future_does_not_authorise_charging(hass):
    _car(hass, soc="60")
    hass.states.async_set(UPDATED, (NOW + timedelta(hours=3)).isoformat())
    report = EvShadow(hass, _updated_config()).evaluate(NOW, None, True, force=True)
    assert report["state"] is None and report["reason"] == "soc_time_invalid"


async def test_a_non_finite_wallbox_value_counts_as_unknown(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="on", intensity="nan", dynamic="on")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, force=True)
    assert report["intents"] == [] and report["intents_withheld"] == "wallbox state incomplete"
    assert report["wallbox"]["intensity_a"] is None


async def test_a_guard_that_is_configured_but_has_not_reported_leaves_the_ceiling_unknown(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="on", dynamic="on")
    config = replace(_wallbox_config(), phase_current_entities=("sensor.a", "sensor.b", "sensor.c"),
                     phase_power_entities=("sensor.d", "sensor.e", "sensor.f"))
    shadow = EvShadow(hass, config)
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, force=True)
    assert report["intents"] == [] and report["intents_withheld"] == "current ceiling unavailable"


async def test_the_forced_charge_plan_respects_a_ceiling_below_the_minimum(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="off", intensity="16", dynamic="on")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, {"ev_paused": False, "ev_current_cap_a": 2.0}, True, force=True)
    assert [(i["target"], i["value"]) for i in report["intents"]] == [("pause", True)]
