"""Per-phase guard, report only: sensor reading, state between ticks, no writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.hallm_energy.config import ConfigInvalidError, EnergyConfig
from custom_components.hallm_energy.energy_core.phase_guard import branch_currents
from custom_components.hallm_energy.guard_flow import GuardShadow
from tests.test_controller import _controller, _fresh_state
from tests.test_executor import FakeInverter
from tests.test_inputs import CONFIG, NOW, OPTIONS, set_telemetry

CURRENTS = ("sensor.i_l1", "sensor.i_l2", "sensor.i_l3")
POWERS = ("sensor.p_l1", "sensor.p_l2", "sensor.p_l3")


def _guarded(config=CONFIG):
    return replace(config, phase_current_entities=CURRENTS, phase_power_entities=POWERS)


def _phases(hass, currents, powers):
    for entity, value in zip(CURRENTS + POWERS, tuple(currents) + tuple(powers), strict=True):
        hass.states.async_set(entity, str(value))


# --- branch split -------------------------------------------------------------------


def test_each_phase_counts_for_the_branch_its_power_points_to():
    assert branch_currents((22.0, 5.0, 9.0), (4000, 500, -2000)) == (22.0, 9.0)
    assert branch_currents((10.0, 5.0, 9.0), (4000, 500, 100)) == (10.0, 0.0)
    assert branch_currents((10.0, 12.0, 9.0), (-4000, -500, -100)) == (0.0, 12.0)


def test_a_missing_phase_value_means_nothing_is_guessed():
    assert branch_currents((10.0, None, 9.0), (1, 1, 1)) == (None, None)
    assert branch_currents((10.0, 5.0, 9.0), (1, None, 1)) == (None, None)
    assert branch_currents((), ()) == (None, None)


# --- configuration ------------------------------------------------------------------


def test_the_guard_is_off_without_phase_sensors():
    assert EnergyConfig.from_options(OPTIONS).phase_current_entities == ()
    assert GuardShadow(CONFIG).evaluate(None, CONFIG, NOW) is None


def test_currents_and_powers_must_come_together():
    with pytest.raises(ConfigInvalidError):
        EnergyConfig.from_options({**OPTIONS, "phase_current_entities": list(CURRENTS)})
    with pytest.raises(ConfigInvalidError):
        EnergyConfig.from_options({**OPTIONS, "phase_current_entities": ["switch.x"],
                                   "phase_power_entities": ["switch.y"]})
    cfg = EnergyConfig.from_options({**OPTIONS, "phase_current_entities": list(CURRENTS),
                                     "phase_power_entities": list(POWERS)})
    assert cfg.phase_current_entities == CURRENTS and cfg.phase_power_entities == POWERS


# --- evaluation ---------------------------------------------------------------------


async def test_quiet_phases_report_unrestricted(hass):
    config = _guarded()
    _phases(hass, (6, 7, 8), (1000, 1200, 900))
    report = GuardShadow(config).evaluate(hass, config, NOW)
    assert report["import_a"] == 8 and report["export_a"] == 0
    assert report["restricted"] is False
    assert report["ev_current_cap_a"] == config.guard_ev_cap_a


async def test_a_lasting_import_excess_steps_the_ev_cap_down(hass):
    config = _guarded()
    guard = GuardShadow(config)
    _phases(hass, (23, 6, 7), (5000, 1000, 900))
    first = guard.evaluate(hass, config, NOW)
    assert first["ev_current_cap_a"] == config.guard_ev_cap_a  # the hold has not elapsed
    _phases(hass, (23, 6, 7), (5000, 1000, 900))
    later = guard.evaluate(hass, config, NOW + timedelta(seconds=12))
    assert later["ev_current_cap_a"] == config.guard_ev_cap_a - 2
    assert later["restricted"] is True


async def test_an_export_excess_lowers_only_the_export_cap(hass):
    config = _guarded()
    guard = GuardShadow(config)
    _phases(hass, (6, 7, 24), (1000, 1000, -6000))
    guard.evaluate(hass, config, NOW)
    _phases(hass, (6, 7, 24), (1000, 1000, -6000))
    report = guard.evaluate(hass, config, NOW + timedelta(seconds=12))
    assert report["export_cap_w"] == config.guard_export_cap_w - 1000
    assert report["ev_current_cap_a"] == config.guard_ev_cap_a and not report["ev_paused"]


async def test_an_unavailable_phase_sensor_freezes_the_restriction(hass):
    config = _guarded()
    guard = GuardShadow(config)
    _phases(hass, (23, 6, 7), (5000, 1000, 900))
    guard.evaluate(hass, config, NOW)
    _phases(hass, (23, 6, 7), (5000, 1000, 900))
    guard.evaluate(hass, config, NOW + timedelta(seconds=12))  # one step down
    hass.states.async_set(CURRENTS[1], "unavailable")
    frozen = guard.evaluate(hass, config, NOW + timedelta(seconds=60))
    assert frozen["import_a"] is None and frozen["ev_current_cap_a"] == config.guard_ev_cap_a - 2


async def test_a_sensor_that_does_not_exist_is_not_a_fresh_reading(hass):
    config = _guarded()
    report = GuardShadow(config).evaluate(hass, config, NOW)
    assert report["import_a"] is None and report["measured_at"] is None


# --- on its own timer ---------------------------------------------------------------


async def _guarded_controller(storage_hass, freezer):
    freezer.move_to(NOW)
    inverter = FakeInverter(storage_hass)
    set_telemetry(storage_hass)
    controller = await _controller(storage_hass, _fresh_state(), live=True)
    controller.config = _guarded(controller.config)
    controller.guard = GuardShadow(controller.config)
    return controller, inverter


async def test_a_step_stores_the_report_and_the_tick_does_not_touch_it(storage_hass, freezer):
    controller, _ = await _guarded_controller(storage_hass, freezer)
    _phases(storage_hass, (23, 6, 7), (5000, 1000, 900))
    controller.guard_step(NOW)
    assert controller.guard_report["import_a"] == 23
    status = await controller.async_tick()
    assert status.result != "error"
    assert controller.guard_report["import_a"] == 23  # the tick only listens, never evaluates


async def test_listeners_hear_only_decision_changes_not_every_reading(storage_hass, freezer):
    controller, _ = await _guarded_controller(storage_hass, freezer)
    heard: list[int] = []
    controller.add_listener(lambda: heard.append(1))
    _phases(storage_hass, (6, 7, 8), (1000, 1000, 1000))
    controller.guard_step(NOW)
    assert len(heard) == 1  # the first report (None -> unrestricted)
    for second in range(1, 6):  # quiet readings, values move every second
        _phases(storage_hass, (6 + second / 10, 7, 8), (1000, 1000, 1000))
        controller.guard_step(NOW + timedelta(seconds=second))
    assert len(heard) == 1
    for second in range(6, 20):  # an excess that lasts: the first step down is one change
        _phases(storage_hass, (23, 7, 8), (5000, 1000, 1000))
        controller.guard_step(NOW + timedelta(seconds=second))
    assert len(heard) == 2
    assert controller.guard_report["restricted"] is True


async def test_a_failing_report_never_raises_and_keeps_the_last_one(storage_hass, freezer):
    controller, _ = await _guarded_controller(storage_hass, freezer)
    _phases(storage_hass, (6, 7, 8), (1000, 1000, 1000))
    controller.guard_step(NOW)
    last = controller.guard_report

    def boom(*_a, **_k):
        raise RuntimeError("no")

    controller.guard.evaluate = boom
    controller.guard_step(NOW + timedelta(seconds=1))
    controller.guard_step(NOW + timedelta(seconds=2))
    assert controller.guard_report is last


async def test_the_tick_writes_no_wallbox_entity(storage_hass, freezer):
    controller, _ = await _guarded_controller(storage_hass, freezer)
    _phases(storage_hass, (23, 6, 7), (5000, 1000, 900))
    status = await controller.async_tick()
    assert not [w for w in status.writes if "wallbox" in w.op.entity_id]


async def _loaded_entry(hass, mqtt_mock, extra):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.hallm_energy.const import DOMAIN

    hass.states.async_set("input_boolean.shadow", "on")
    hass.states.async_set("input_boolean.grid_recovery", "on")
    FakeInverter(hass)
    set_telemetry(hass, soc="50.0")
    entry = MockConfigEntry(domain=DOMAIN, options={**OPTIONS, **extra})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_the_guard_timer_runs_only_when_phase_sensors_are_configured(
        storage_hass, mqtt_mock):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hass = storage_hass
    _phases(hass, (6, 7, 8), (1000, 1000, 1000))
    entry = await _loaded_entry(hass, mqtt_mock, {
        "phase_current_entities": list(CURRENTS), "phase_power_entities": list(POWERS)})
    controller = entry.runtime_data.controller
    controller.guard_report = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert controller.guard_report is not None and controller.guard_report["import_a"] == 8
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_without_phase_sensors_the_guard_never_runs(storage_hass, mqtt_mock):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hass = storage_hass
    entry = await _loaded_entry(hass, mqtt_mock, {})
    controller = entry.runtime_data.controller
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert controller.guard_report is None
    assert await hass.config_entries.async_unload(entry.entry_id)
