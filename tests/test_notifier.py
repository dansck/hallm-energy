"""Notifications to the owner: edge-triggered with a repeat, never touching the loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.hallm_energy import notifier as notifier_module
from custom_components.hallm_energy.config import ConfigInvalidError, EnergyConfig
from custom_components.hallm_energy.notifier import REPEAT_AFTER, Notifier
from tests.test_controller import (
    CAPS,
    GRID,
    _charging_on,
    _controller,
)
from tests.test_executor import FakeInverter
from tests.test_inputs import NOW, OPTIONS, set_telemetry
from tests.test_lifecycle import activated, empty_state, set_reconcile

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


async def test_an_event_is_rate_limited_per_kind(hass):
    fake = FakeNotify(hass)
    n = Notifier(hass, "notify.mobile")
    assert await n.async_notify_once("stop", "stopped", T0) is True
    assert await n.async_notify_once("stop", "stopped", T0 + timedelta(minutes=5)) is False
    assert await n.async_notify_once("stop", "stopped", T0 + timedelta(minutes=11)) is True
    assert len(fake.calls) == 2


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


# --- in the loop -------------------------------------------------------------------------------


async def test_an_outside_switch_off_of_a_running_grant_is_reported(storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    for cap in CAPS:
        storage_hass.states.async_set(cap, "25")
    _charging_on(storage_hass)
    controller = await _controller(storage_hass, set_reconcile(activated(), "ready", None),
                                   live=False)
    controller.notifier = Notifier(storage_hass, "notify.mobile")
    await controller.async_tick()
    assert fake.calls == []
    storage_hass.states.async_set(GRID, "off")        # a person switches grid charging off
    await controller.async_tick()
    assert any("switched off from outside" in c["message"] for c in fake.calls)
    count = len(fake.calls)
    await controller.async_tick()                      # still blocked: no second message
    assert len(fake.calls) == count


async def test_a_loop_error_is_reported_and_does_not_break_the_loop(storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    controller = await _controller(storage_hass, empty_state())
    controller.notifier = Notifier(storage_hass, "notify.mobile")
    with patch.object(controller.executor, "async_cycle", side_effect=RuntimeError("boom")):
        status = await controller.async_tick()
    assert status.result == "error"
    assert any("control loop hit an error" in c["message"] for c in fake.calls)


async def test_a_broken_notifier_never_breaks_a_tick(storage_hass, fake_inverter):
    FakeNotify(storage_hass, fail=True)
    controller = await _controller(storage_hass, empty_state())
    controller.notifier = Notifier(storage_hass, "notify.mobile")
    with patch.object(controller.executor, "async_cycle", side_effect=RuntimeError("boom")):
        status = await controller.async_tick()
    assert status.result == "error"


async def test_the_safety_stop_service_notifies(storage_hass, fake_inverter):
    fake = FakeNotify(storage_hass)
    controller = await _controller(storage_hass, set_reconcile(activated(), "ready", None))
    controller.notifier = Notifier(storage_hass, "notify.mobile")
    await controller.async_safety_stop()
    assert any("safety stop" in c["message"] for c in fake.calls)
