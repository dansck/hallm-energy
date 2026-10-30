"""Notifications to the owner: edge-triggered with a repeat, never touching the loop."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.hallm_energy import notifier as notifier_module
from custom_components.hallm_energy.config import ConfigInvalidError, EnergyConfig
from custom_components.hallm_energy.notifier import REPEAT_AFTER, Notifier
from tests.test_controller import (
    GRID,
    _controller,
)
from tests.test_executor import FakeInverter
from tests.test_inputs import NOW, OPTIONS, set_telemetry
from tests.test_lifecycle import empty_state

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake_inverter(storage_hass, freezer):
    freezer.move_to(NOW)
    device = FakeInverter(storage_hass)
    set_telemetry(storage_hass)
    return device



class FakeNotify:
    def __init__(self, hass, name="mobile", *, fail=False, stall=False):
        self.calls: list[dict] = []
        self.fail, self.stall = fail, stall
        hass.services.async_register("notify", name, self._handle)

    async def _handle(self, call):
        self.calls.append(dict(call.data))
        if self.stall:
            await asyncio.sleep(60)
        if self.fail:
            raise HomeAssistantError("no phone")


async def test_a_condition_notifies_once_then_again_after_the_repeat_interval(hass):
    fake = FakeNotify(hass)
    n = Notifier(hass, "notify.mobile")
    assert await n.async_observe("x", True, "boom", T0) is True
    assert await n.async_observe("x", True, "boom", T0 + timedelta(hours=1)) is False
    assert await n.async_observe("x", True, "boom", T0 + REPEAT_AFTER) is True
    assert [c["message"] for c in fake.calls] == ["boom", "boom"]
    assert fake.calls[0]["title"] == "Hallm Energy"


async def test_a_cleared_condition_rearms_so_the_next_onset_notifies_at_once(hass):
    fake = FakeNotify(hass)
    n = Notifier(hass, "notify.mobile")
    await n.async_observe("x", True, "one", T0)
    await n.async_observe("x", False, "", T0 + timedelta(minutes=1))
    assert await n.async_observe("x", True, "two", T0 + timedelta(minutes=2)) is True
    assert [c["message"] for c in fake.calls] == ["one", "two"]


async def test_kinds_are_independent(hass):
    fake = FakeNotify(hass)
    n = Notifier(hass, "notify.mobile")
    await n.async_observe("a", True, "A", T0)
    await n.async_observe("b", True, "B", T0)
    assert len(fake.calls) == 2


async def test_without_a_service_nothing_is_sent(hass):
    fake = FakeNotify(hass)
    n = Notifier(hass, None)
    assert n.enabled is False
    assert await n.async_observe("x", True, "boom", T0) is False
    assert fake.calls == []


async def test_a_failing_service_is_logged_not_raised_and_not_hammered(hass):
    fake = FakeNotify(hass, fail=True)
    n = Notifier(hass, "notify.mobile")
    assert await n.async_observe("x", True, "boom", T0) is False
    assert "HomeAssistantError" in n.last_error
    assert await n.async_observe("x", True, "boom", T0 + timedelta(minutes=1)) is False
    assert len(fake.calls) == 1                      # the failure did not cause a retry storm


async def test_a_missing_service_is_survived(hass):
    n = Notifier(hass, "notify.not_there")
    assert await n.async_observe("x", True, "boom", T0) is False
    assert n.last_error is not None


async def test_a_stalled_service_is_cut_off_by_the_timeout(hass):
    FakeNotify(hass, stall=True)
    n = Notifier(hass, "notify.mobile")
    with patch.object(notifier_module, "CALL_TIMEOUT_S", 0.05):
        assert await n.async_observe("x", True, "boom", T0) is False
    assert "TimeoutError" in n.last_error


@pytest.mark.parametrize("value", ["mobile_app_phone", "notify.", "notify.Bad Name", "light.x", 5])
def test_the_service_name_is_validated(value):
    options = {**OPTIONS, "notify_service": value}
    with pytest.raises(ConfigInvalidError):
        EnergyConfig.from_options(options)


def test_a_valid_service_name_is_accepted_and_optional():
    assert EnergyConfig.from_options(
        {**OPTIONS, "notify_service": "notify.mobile_app_phone"}
    ).notify_service == "notify.mobile_app_phone"
    assert EnergyConfig.from_options(OPTIONS).notify_service is None


# --- delay: a flicker is not an event ---------------------------------------------------------


async def test_a_condition_must_last_for_its_delay_before_it_notifies(hass):
    fake = FakeNotify(hass)
    n = Notifier(hass, "notify.mobile")
    delay = timedelta(minutes=1)
    assert await n.async_observe("x", True, "boom", T0, after=delay) is False
    at = lambda seconds: T0 + timedelta(seconds=seconds)  # noqa: E731
    assert await n.async_observe("x", True, "boom", at(30), after=delay) is False
    assert await n.async_observe("x", True, "boom", at(60), after=delay) is True
    assert len(fake.calls) == 1


async def test_a_flicker_restarts_the_delay(hass):
    fake = FakeNotify(hass)
    n = Notifier(hass, "notify.mobile")
    delay = timedelta(minutes=1)
    await n.async_observe("x", True, "boom", T0, after=delay)
    await n.async_observe("x", False, "", T0 + timedelta(seconds=40), after=delay)   # it cleared
    assert await n.async_observe("x", True, "boom", T0 + timedelta(seconds=50),
                                 after=delay) is False                               # a new onset
    assert await n.async_observe("x", True, "boom", T0 + timedelta(seconds=100),
                                 after=delay) is False                               # only 50 s
    assert await n.async_observe("x", True, "boom", T0 + timedelta(seconds=111),
                                 after=delay) is True
    assert len(fake.calls) == 1


# --- in the loop: critical events only ----------------------------------------------------------


def _controller_with_notifier(controller, hass):
    controller.notifier = Notifier(hass, "notify.mobile")
    return controller


async def test_a_grid_outage_is_reported_after_a_minute_not_at_once(storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    storage_hass.states.async_set("binary_sensor.grid_relay", "on")
    controller = _controller_with_notifier(
        await _controller(storage_hass, empty_state(), live=False), storage_hass)
    controller.config = replace(controller.config, grid_relay_entity="binary_sensor.grid_relay")
    await controller.async_tick(NOW)
    assert fake.calls == []
    storage_hass.states.async_set("binary_sensor.grid_relay", "off")
    await controller.async_tick(NOW + timedelta(seconds=10))
    assert fake.calls == []                                 # a flicker of the relay
    await controller.async_tick(NOW + timedelta(seconds=75))
    assert [("Grid outage" in c["message"]) for c in fake.calls] == [True]
    await controller.async_tick(NOW + timedelta(seconds=105))
    assert len(fake.calls) == 1                             # not again while it lasts
    storage_hass.states.async_set("binary_sensor.grid_relay", "on")
    await controller.async_tick(NOW + timedelta(seconds=135))
    storage_hass.states.async_set("binary_sensor.grid_relay", "off")
    await controller.async_tick(NOW + timedelta(seconds=200))
    await controller.async_tick(NOW + timedelta(seconds=270))
    assert len(fake.calls) == 2                             # a new outage notifies again


async def test_an_unreachable_inverter_is_reported_after_five_minutes(storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    controller = _controller_with_notifier(
        await _controller(storage_hass, empty_state(), live=False), storage_hass)
    storage_hass.states.async_set("sensor.bms_soc", "unavailable")
    await controller.async_tick(NOW)
    await controller.async_tick(NOW + timedelta(minutes=4))
    assert fake.calls == []
    await controller.async_tick(NOW + timedelta(minutes=5, seconds=10))
    assert [("inverter cannot be reached" in c["message"]) for c in fake.calls] == [True]


async def test_unreachable_charge_settings_count_as_an_unreachable_inverter(
        storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    controller = _controller_with_notifier(
        await _controller(storage_hass, empty_state(), live=False), storage_hass)
    storage_hass.states.async_set(GRID, "unavailable")
    await controller.async_tick(NOW)
    await controller.async_tick(NOW + timedelta(minutes=5, seconds=10))
    assert any("inverter cannot be reached" in c["message"] for c in fake.calls)


async def test_everything_else_does_not_notify(storage_hass, fake_inverter):
    """A loop error, a manual switch-off, a blocked state … are handled by the integration."""
    fake = FakeNotify(storage_hass)
    controller = _controller_with_notifier(
        await _controller(storage_hass, empty_state()), storage_hass)
    with patch.object(controller.executor, "async_cycle", side_effect=RuntimeError("boom")):
        status = await controller.async_tick(NOW)
        await controller.async_tick(NOW + timedelta(hours=1))
    assert status.result == "error" and fake.calls == []
    await controller.async_safety_stop()
    assert fake.calls == []


async def test_a_broken_notifier_never_breaks_a_tick(storage_hass, fake_inverter):
    FakeNotify(storage_hass, fail=True)
    controller = _controller_with_notifier(
        await _controller(storage_hass, empty_state(), live=False), storage_hass)
    storage_hass.states.async_set("sensor.bms_soc", "unavailable")
    await controller.async_tick(NOW)
    status = await controller.async_tick(NOW + timedelta(minutes=6))
    assert status.result != "error"


# --- grid voltage out of range (replaces a phone alert that watched the same thresholds) ---------

VOLTAGES = ("sensor.v_l1", "sensor.v_l2", "sensor.v_l3")


def _with_voltage(controller):
    controller.config = replace(controller.config, grid_voltage_entities=VOLTAGES)
    return controller


def _set_voltages(hass, l1="230", l2="231", l3="229"):
    for entity, value in zip(VOLTAGES, (l1, l2, l3), strict=True):
        hass.states.async_set(entity, value)


@pytest.mark.parametrize(("l1", "l2", "l3", "culprit"), [
    ("246", "231", "229", "sensor.v_l1"),
    ("230", "209.9", "229", "sensor.v_l2"),
    ("230", "231", "251.3", "sensor.v_l3"),
])
async def test_a_phase_voltage_out_of_range_is_reported_after_thirty_seconds(
        storage_hass, fake_inverter, l1, l2, l3, culprit):
    fake = FakeNotify(storage_hass)
    controller = _controller_with_notifier(
        _with_voltage(await _controller(storage_hass, empty_state(), live=False)), storage_hass)
    _set_voltages(storage_hass)
    await controller.async_tick(NOW)
    assert fake.calls == []
    _set_voltages(storage_hass, l1, l2, l3)
    await controller.async_tick(NOW + timedelta(seconds=10))
    assert fake.calls == []                                   # not yet: 30 s
    await controller.async_tick(NOW + timedelta(seconds=45))
    assert len(fake.calls) == 1
    assert "Grid voltage out of range" in fake.calls[0]["message"]
    assert culprit in fake.calls[0]["message"]


async def test_a_voltage_spike_that_passes_is_not_reported(storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    controller = _controller_with_notifier(
        _with_voltage(await _controller(storage_hass, empty_state(), live=False)), storage_hass)
    _set_voltages(storage_hass, l1="250")
    await controller.async_tick(NOW)
    _set_voltages(storage_hass)                               # back to normal
    await controller.async_tick(NOW + timedelta(seconds=40))
    assert fake.calls == []


async def test_normal_voltages_and_unavailable_sensors_do_not_notify(storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    controller = _controller_with_notifier(
        _with_voltage(await _controller(storage_hass, empty_state(), live=False)), storage_hass)
    _set_voltages(storage_hass, "245", "210", "230")          # the limits themselves are fine
    await controller.async_tick(NOW)
    await controller.async_tick(NOW + timedelta(minutes=2))
    _set_voltages(storage_hass, "unavailable", "unknown", "x")
    await controller.async_tick(NOW + timedelta(minutes=3))
    await controller.async_tick(NOW + timedelta(minutes=4))
    assert not any("Grid voltage" in c["message"] for c in fake.calls)


async def test_without_voltage_sensors_nothing_is_watched(storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    controller = _controller_with_notifier(
        await _controller(storage_hass, empty_state(), live=False), storage_hass)
    _set_voltages(storage_hass, "300", "300", "300")
    await controller.async_tick(NOW)
    await controller.async_tick(NOW + timedelta(minutes=2))
    assert fake.calls == []


@pytest.mark.parametrize("value", ["sensor.v", [5], ["light.v"], ["v_l1"]])
def test_voltage_sensors_are_validated(value):
    with pytest.raises(ConfigInvalidError):
        EnergyConfig.from_options({**OPTIONS, "grid_voltage_entities": value})


def test_voltage_sensors_are_optional_and_kept_as_a_tuple():
    assert EnergyConfig.from_options(OPTIONS).grid_voltage_entities == ()
    cfg = EnergyConfig.from_options({**OPTIONS, "grid_voltage_entities": list(VOLTAGES)})
    assert cfg.grid_voltage_entities == VOLTAGES
