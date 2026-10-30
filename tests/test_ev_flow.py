"""EV charging state, report only: entity reading, calendar requests, decisions, no writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import SupportsResponse

from custom_components.hallm_energy.config import ConfigInvalidError, EnergyConfig
from custom_components.hallm_energy.energy_core.ev_plan import TariffFacts, WallboxFacts
from custom_components.hallm_energy.ev_flow import EvShadow
from custom_components.hallm_energy.inputs import offpeak_hours_between
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


async def test_a_soc_below_the_hold_level_is_routine_charging_and_at_the_level_idle(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _, report = await _state(hass)
    assert report["state"] == "routine"
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
    assert shadow.evaluate(NOW, None, True)["state"] == "routine"
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
    assert controller.ev_force is False and controller.ev_report["state"] == "routine"
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


async def test_a_car_at_the_hold_level_with_a_running_wallbox_would_be_paused(hass):
    _car(hass, soc="85")
    _calendar(hass, [])
    _wallbox(hass, paused="off")
    _, report = await _state(hass, config=_wallbox_config())
    assert report["state"] == "idle" and _intents(report) == {("pause", True)}
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


async def test_a_state_that_needs_the_surplus_or_the_battery_is_withheld_not_guessed(hass):
    from custom_components.hallm_energy.energy_core.ev_plan import plan_ev_intents

    intents, why = plan_ev_intents("pv_charge", wallbox=WallboxFacts(), cap_a=16.0)
    assert intents == () and "not evaluated" in why


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
async def test_an_invalid_soc_is_only_a_missing_input_it_neither_stops_nor_starts_charging(
        hass, raw):
    _car(hass, soc=raw)
    _calendar(hass, [])
    shadow = EvShadow(hass, _ev_config())
    await shadow.async_refresh_calendar(NOW)
    forced = shadow.evaluate(NOW, None, True, force=True)
    assert forced["state"] == "force_charge" and forced["soc_reason"] in (
        "soc_invalid", "soc_unavailable")
    idle = shadow.evaluate(NOW, None, True)
    assert idle["state"] is None and idle["reason"] in ("soc_invalid", "soc_unavailable")


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
    assert report["state"] == "routine" and report["soc_age_min"] == 30


@pytest.mark.parametrize(("value", "reason"), [
    ("unavailable", "soc_time_unknown"), ("not a time", "soc_time_unknown"),
])
async def test_an_unusable_reading_time_makes_the_level_unknown_not_a_stop(hass, value, reason):
    _car(hass, soc="60")
    hass.states.async_set(UPDATED, value)
    shadow = EvShadow(hass, _updated_config())
    assert shadow.evaluate(NOW, None, True)["reason"] == reason
    forced = shadow.evaluate(NOW, None, True, force=True)
    assert forced["state"] == "force_charge" and forced["soc_reason"] == reason


async def test_a_reading_time_from_the_future_makes_the_level_unknown(hass):
    _car(hass, soc="60")
    hass.states.async_set(UPDATED, (NOW + timedelta(hours=3)).isoformat())
    shadow = EvShadow(hass, _updated_config())
    assert shadow.evaluate(NOW, None, True)["reason"] == "soc_time_invalid"
    assert shadow.evaluate(NOW, None, True, force=True)["state"] == "force_charge"


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


# --- the tariff ---------------------------------------------------------------------------------

H = timedelta(hours=1)


def _iv(start_h, end_h, nt):
    return (NOW + start_h * H, NOW + end_h * H, nt)


def test_off_peak_hours_are_counted_inside_the_span_and_coverage_is_reported():
    schedule = (_iv(-2, 4, True), _iv(4, 20, False), _iv(20, 30, True))
    assert offpeak_hours_between(schedule, NOW, NOW + 10 * H) == (4.0, True)
    assert offpeak_hours_between(schedule, NOW, NOW + 26 * H) == (10.0, True)
    assert offpeak_hours_between(schedule, NOW, NOW + 40 * H) == (14.0, False)  # a lower bound
    assert offpeak_hours_between(schedule, NOW + 5 * H, NOW + 6 * H) == (0.0, True)
    assert offpeak_hours_between(None, NOW, NOW + H) == (None, False)
    assert offpeak_hours_between(schedule, NOW, NOW) == (0.0, True)


def test_a_gap_in_the_schedule_is_not_coverage():
    schedule = (_iv(0, 2, True), _iv(5, 8, True))
    assert offpeak_hours_between(schedule, NOW, NOW + 8 * H) == (5.0, False)


async def _controller_with_tariff(storage_hass, freezer, *, relay, schedule):
    from tests.test_inputs import set_telemetry as telemetry

    freezer.move_to(NOW)
    FakeInverter(storage_hass)
    telemetry(storage_hass, relay=relay, schedule=schedule)
    controller = await _controller(storage_hass, _fresh_state(), live=True)
    return controller


def _sched(*items):
    return [{"start": a.isoformat(), "end": b.isoformat(), "tariff": "NT" if nt else "VT"}
            for a, b, nt in items]


async def test_off_peak_now_needs_the_relay_and_the_schedule_to_agree(storage_hass, freezer):
    night = _sched(_iv(-1, 5, True), _iv(5, 40, False))
    controller = await _controller_with_tariff(storage_hass, freezer, relay="on", schedule=night)
    assert controller._tariff_facts(NOW, None).nt_now is True
    storage_hass.states.async_set("binary_sensor.hdo_nt", "off")
    assert controller._tariff_facts(NOW, None).nt_now is None  # they disagree: unknown
    storage_hass.states.async_set("binary_sensor.hdo_nt", "unavailable")
    assert controller._tariff_facts(NOW, None).nt_now is None
    day = _sched(_iv(-1, 5, False), _iv(5, 40, True))
    controller = await _controller_with_tariff(storage_hass, freezer, relay="off", schedule=day)
    assert controller._tariff_facts(NOW, None).nt_now is False


async def test_a_relay_that_says_off_peak_without_a_schedule_is_not_believed(storage_hass, freezer):
    controller = await _controller_with_tariff(
        storage_hass, freezer, relay="on", schedule=_sched(_iv(10, 20, True)))
    assert controller._tariff_facts(NOW, None).nt_now is None


async def test_the_deadline_hours_come_from_the_schedule(storage_hass, freezer):
    night = _sched(_iv(-1, 5, True), _iv(5, 20, False), _iv(20, 30, True))
    controller = await _controller_with_tariff(storage_hass, freezer, relay="on", schedule=night)
    facts = controller._tariff_facts(NOW, NOW + 24 * H)
    assert facts == TariffFacts(True, 9.0, True)


async def test_a_routine_charge_in_peak_is_paused_and_in_off_peak_it_runs(hass):
    _car(hass, soc="33")
    _calendar(hass, [])
    _wallbox(hass, paused="off", intensity="16", dynamic="off")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    peak = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(False, None, False))
    assert peak["state"] == "mobility_floor" and peak["grid_charge"]["allowed"] is False
    assert {(i["target"], i["value"]) for i in peak["intents"]} == {("pause", True)}
    off = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(True, None, False))
    assert off["grid_charge"]["allowed"] is True and off["deye_mode_required"] == "to load"
    assert off["intents"] == []  # already running at full power


async def test_a_deadline_in_peak_charges_only_when_off_peak_time_runs_short(hass):
    _car(hass, soc="50")
    start = (NOW + timedelta(hours=6)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 100"}])
    _wallbox(hass, paused="on", intensity="16", dynamic="off")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    enough = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(False, 5.0, True))
    short = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(False, 1.0, True))
    assert enough["state"] == short["state"] == "deadline"
    assert enough["grid_charge"]["allowed"] is False and enough["intents"] == []  # stays paused
    assert short["grid_charge"]["allowed"] is True
    assert [(i["target"], i["value"]) for i in short["intents"]] == [("pause", False)]


async def test_an_unknown_tariff_withholds_the_plan_instead_of_guessing(hass):
    _car(hass, soc="33")
    _calendar(hass, [])
    _wallbox(hass)
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(None, None, False))
    assert report["intents"] == [] and report["intents_withheld"] == "tariff unknown"
    assert report["grid_charge"]["allowed"] is None and "deye_mode_required" not in report


def test_the_peak_permission_is_a_boolean_option():
    assert EnergyConfig.from_options(OPTIONS).ev_deadline_allow_peak is True
    assert EnergyConfig.from_options(
        {**OPTIONS, "ev_deadline_allow_peak": False}).ev_deadline_allow_peak is False
    with pytest.raises(ConfigInvalidError):
        EnergyConfig.from_options({**OPTIONS, "ev_deadline_allow_peak": "yes"})


# --- the level is an input to calculations only ----------------------------------------------


async def test_without_a_level_a_request_still_charges_in_off_peak_and_waits_in_peak(hass):
    _car(hass, soc="unavailable")
    start = (NOW + timedelta(hours=6)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 100"}])
    _wallbox(hass, paused="on", intensity="16", dynamic="off")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    night = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(True, 5.0, True))
    assert night["state"] == "deadline" and night["soc_reason"] == "soc_unavailable"
    assert "energy_needed_kwh" not in night["request"]
    assert [(i["target"], i["value"]) for i in night["intents"]] == [("pause", False)]
    day = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(False, 5.0, True))
    assert day["intents"] == [] and "unknown" in day["intents_withheld"]


async def test_without_a_level_and_without_a_reason_to_charge_nothing_changes(hass):
    _car(hass, soc="nan")
    _calendar(hass, [])
    _wallbox(hass, paused="off", intensity="16", dynamic="off")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(False, None, False))
    assert report["state"] is None and "intents" not in report  # a running charge is left alone


async def test_an_absent_car_or_a_protection_hold_without_a_level_too(hass):
    _car(hass, soc="nan", plugged="off")
    _calendar(hass, [])
    shadow = EvShadow(hass, _ev_config())
    await shadow.async_refresh_calendar(NOW)
    assert shadow.evaluate(NOW, None, True)["state"] == "away"
    _car(hass, soc="nan")
    assert shadow.evaluate(NOW, {"ev_paused": True}, True)["state"] == "curtailed"


async def test_over_the_trip_level_the_plan_may_lower_the_load_but_never_raise_it(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="off", intensity="6", maxi="16", dynamic="off")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    guard = {"stale": False, "ev_paused": False, "ev_current_cap_a": 16.0, "import_a": 23.0}
    over = shadow.evaluate(NOW, guard, True, force=True)
    assert all(i["target"] != "intensity" or i["value"] <= 6.0 for i in over["intents"])
    assert all(i["target"] != "max_intensity" or i["value"] <= 6.0 for i in over["intents"])
    ok = shadow.evaluate(NOW, {**guard, "import_a": 12.0}, True, force=True)
    assert ("intensity", 16.0) in {(i["target"], i["value"]) for i in ok["intents"]}


async def test_over_the_trip_level_the_ceiling_still_comes_down_with_the_guard(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="off", intensity="16", maxi="16", dynamic="off")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    guard = {"stale": False, "ev_paused": False, "ev_current_cap_a": 10.0, "import_a": 23.0}
    report = shadow.evaluate(NOW, guard, True, force=True)
    steps = [(i["target"], i["value"]) for i in report["intents"]]
    assert steps == [("intensity", 10.0), ("max_intensity", 10.0)]


# --- hold at 80, the final top-up just before departure, the limit on the car -------------------

LIMIT = "number.car_limit"


def _limit_config(**over):
    return replace(_wallbox_config(), ev_limit_entity=LIMIT, **over)


async def test_routine_charging_runs_in_off_peak_and_waits_in_peak(hass):
    _car(hass, soc="60")
    _calendar(hass, [])
    _wallbox(hass, paused="on", intensity="16", dynamic="off")
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    night = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(True, None, False))
    assert night["state"] == "routine" and night["grid_charge"]["allowed"] is True
    assert {(i["target"], i["value"]) for i in night["intents"]} == {("pause", False)}
    _wallbox(hass, paused="off", intensity="16", dynamic="off")
    day = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(False, None, False))
    assert day["state"] == "routine" and day["grid_charge"]["allowed"] is False
    assert {(i["target"], i["value"]) for i in day["intents"]} == {("pause", True)}


async def test_a_request_above_the_hold_level_waits_for_the_last_hours_before_departure(hass):
    _car(hass, soc="80")
    start = (NOW + timedelta(hours=20)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 100"}])
    _wallbox(hass, paused="off", intensity="16", dynamic="off")
    hass.states.async_set(LIMIT, "80")
    shadow = EvShadow(hass, _limit_config())
    await shadow.async_refresh_calendar(NOW)
    early = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(True, 15.0, True))
    assert early["state"] == "deadline" and early["grid_charge"]["allowed"] is False
    assert "shortly before departure" in early["grid_charge"]["reason"]
    assert {(i["target"], i["value"]) for i in early["intents"]} == {("pause", True)}
    assert early["car_limit"] == {"desired": None, "current": 80.0}
    # 2.5 h before departure: the limit is raised ahead of time, charging has not started yet
    ahead = shadow.evaluate(NOW + timedelta(hours=17, minutes=30), None, True,
                            tariff=lambda d: TariffFacts(True, 2.5, True))
    assert ahead["car_limit"]["desired"] == 100.0 and ahead["grid_charge"]["allowed"] is False
    due = shadow.evaluate(NOW + timedelta(hours=18), None, True,
                          tariff=lambda d: TariffFacts(True, 2.0, True))
    assert due["grid_charge"]["allowed"] is True and due["car_limit"]["desired"] == 100.0


async def test_below_the_hold_level_a_request_charges_at_once(hass):
    _car(hass, soc="60")
    start = (NOW + timedelta(hours=20)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 100"}])
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(True, 15.0, True))
    assert report["state"] == "deadline" and report["grid_charge"]["allowed"] is True


async def test_a_request_at_the_hold_level_needs_no_waiting_and_no_limit_change(hass):
    _car(hass, soc="50")
    start = (NOW + timedelta(hours=20)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 80"}])
    hass.states.async_set(LIMIT, "80")
    shadow = EvShadow(hass, _limit_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(True, 15.0, True))
    assert report["grid_charge"]["allowed"] is True
    assert report["car_limit"] == {"desired": None, "current": 80.0}


async def test_the_limit_on_the_car_is_never_asked_to_come_back_down(hass):
    _car(hass, soc="100")
    _calendar(hass, [])
    hass.states.async_set(LIMIT, "100")
    shadow = EvShadow(hass, _limit_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True)
    assert report["car_limit"] == {"desired": None, "current": 100.0}


# --- wait for sun only when it is coming --------------------------------------------------------


async def _sun_state(hass, soc, sun):
    _car(hass, soc=soc)
    _calendar(hass, [])
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    return shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(True, None, False),
                           sun=sun)


async def test_with_sun_coming_and_enough_energy_the_car_waits_for_it(hass):
    report = await _sun_state(hass, "60", (1, 22.0, True))
    assert report["state"] == "deferred"
    assert report["sun"] == {"first_window_day": 1, "window_kwh": 22.0,
                             "forecast_covers_span": True}
    assert "grid_charge" not in report  # waiting: no charging from the grid is asked for


async def test_with_no_sun_in_the_coming_days_the_level_is_held_from_off_peak(hass):
    report = await _sun_state(hass, "60", (None, 0.0, True))
    assert report["state"] == "routine" and report["grid_charge"]["allowed"] is True


async def test_without_a_forecast_nobody_waits_for_sun_that_cannot_be_vouched_for(hass):
    report = await _sun_state(hass, "60", None)
    assert report["state"] == "routine" and "sun" not in report


async def test_waiting_for_sun_is_bounded_by_the_energy_for_the_driving_until_then(hass):
    # sun only the day after tomorrow: 30 % minimum + 3 days of 10 % = 60 %
    assert (await _sun_state(hass, "55", (2, 22.0, True)))["state"] == "mobility_floor"
    assert (await _sun_state(hass, "65", (2, 22.0, True)))["state"] == "deferred"
    # sun today: 30 % + 1 day = 40 %
    assert (await _sun_state(hass, "35", (0, 12.0, True)))["state"] == "mobility_floor"
    assert (await _sun_state(hass, "45", (0, 12.0, True)))["state"] == "deferred"


async def test_a_request_with_a_deadline_does_not_wait_for_sun(hass):
    _car(hass, soc="50")
    start = (NOW + timedelta(hours=20)).isoformat()
    _calendar(hass, [{"start": start, "end": start, "summary": "EV 80"}])
    shadow = EvShadow(hass, _wallbox_config())
    await shadow.async_refresh_calendar(NOW)
    report = shadow.evaluate(NOW, None, True, tariff=lambda d: TariffFacts(True, 15.0, True),
                             sun=(1, 22.0, True))
    assert report["state"] == "deadline" and report["grid_charge"]["allowed"] is True
