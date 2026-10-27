"""Control loop against a real test Home Assistant and the fake inverter:
provisioning, startup reconcile, leaving live mode, external switch-off."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.hallm_energy.const import STORAGE_KEY
from custom_components.hallm_energy.controller import EnergyController, ProvisionError
from custom_components.hallm_energy.device_io import DeviceIO
from custom_components.hallm_energy.lifecycle import new_installation_state, set_reconcile
from custom_components.hallm_energy.runtime import EnergyRuntime
from custom_components.hallm_energy.store import LocalStateStore
from tests.test_executor import CAPS, CES, FEATURE, GRID, NIGHT_SLOT, SHADOW, FakeInverter
from tests.test_inputs import CONFIG, NOW, set_telemetry
from tests.test_lifecycle import COUNTER, activated, empty_state


@pytest.fixture
def inverter(storage_hass, freezer):
    freezer.move_to(NOW)
    inverter = FakeInverter(storage_hass)
    set_telemetry(storage_hass)
    return inverter


def _gates(hass, *, live: bool) -> None:
    hass.states.async_set(SHADOW, "off" if live else "on")
    hass.states.async_set(FEATURE, "on")


async def _controller(hass, state, *, live=False) -> EnergyController:
    _gates(hass, live=live)
    runtime = EnergyRuntime(hass, LocalStateStore(hass, version=1, key=STORAGE_KEY))
    await runtime.async_commit(state)
    device = DeviceIO(hass, call_timeout_s=0.3, readback_timeout_s=0.3, poll_interval_s=0.02)
    return EnergyController(hass, runtime, CONFIG, io=device)


def _charging_on(hass) -> None:
    hass.states.async_set(GRID, "on")
    hass.states.async_set(CES[NIGHT_SLOT], "on")


def _fresh_state():
    return new_installation_state(installation_id="inst-1", policy_version="p",
                                  core_version="c")


# --- provisioning ----------------------------------------------------------------


async def test_unprovisioned_state_only_reconciles_and_writes_nothing(storage_hass, inverter):
    controller = await _controller(storage_hass, _fresh_state(), live=True)
    status = await controller.async_tick()
    assert status.result == "reconcile:required"
    assert status.reason == "not_provisioned"
    assert status.writes == [] and inverter.calls == []


async def test_provision_trusts_counter_then_reconciles_ready(storage_hass, inverter):
    controller = await _controller(storage_hass, _fresh_state())
    await controller.async_provision()
    counter = controller.runtime.state.counter_state
    assert controller.runtime.state.provisioning_state == "ready"
    assert (counter.counter_id, counter.counter_epoch) == (COUNTER, "e1")
    assert counter.last_trusted_raw_dc_kwh == 7204.0
    assert controller.runtime.state.reconcile_state.reason_code == "provisioned"

    status = await controller.async_tick()
    assert controller.runtime.state.reconcile_state.value == "ready"
    assert status.result == "cycle"
    assert inverter.calls == []


async def test_provision_refused_when_counter_is_unknown(storage_hass, inverter):
    storage_hass.states.async_set(COUNTER, "unknown")
    controller = await _controller(storage_hass, _fresh_state())
    with pytest.raises(ProvisionError):
        await controller.async_provision()
    assert controller.runtime.state.provisioning_state == "uninitialized"


async def test_provision_only_once(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state())
    with pytest.raises(ProvisionError):
        await controller.async_provision()


# --- startup reconcile -----------------------------------------------------------


async def test_reconcile_in_shadow_reports_stop_and_writes_nothing(storage_hass, inverter):
    _charging_on(storage_hass)
    state = set_reconcile(empty_state(), "required", "startup")
    controller = await _controller(storage_hass, state, live=False)
    status = await controller.async_tick()
    assert status.reason == "charge_without_grant"
    assert (GRID, False) in [(op.entity_id, op.value) for op in status.intents]
    assert inverter.calls == []
    assert inverter.value(GRID) == "on"
    assert controller.runtime.state.reconcile_state.value == "required"


async def test_reconcile_live_stops_first_then_becomes_ready(storage_hass, inverter):
    _charging_on(storage_hass)
    state = set_reconcile(empty_state(), "required", "startup")
    controller = await _controller(storage_hass, state, live=True)
    await controller.async_tick()
    assert inverter.calls[0] == (GRID, False)
    assert inverter.value(GRID) == "off"
    await controller.async_tick()
    assert controller.runtime.state.reconcile_state.value == "ready"


async def test_readback_unavailable_blocks_cycle(storage_hass, inverter):
    storage_hass.states.async_set(GRID, "unavailable")
    storage_hass.states.async_set(CES[NIGHT_SLOT], "unavailable")
    state = set_reconcile(empty_state(), "required", "startup")
    controller = await _controller(storage_hass, state, live=True)
    status = await controller.async_tick()
    assert status.reason == "readback_unavailable"
    assert controller.runtime.state.reconcile_state.value == "required"


# --- live mode ---------------------------------------------------------------------


async def test_leaving_live_mode_stops_once(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state(), live=True)
    await controller.async_tick()
    _charging_on(storage_hass)  # e.g. someone enabled charging meanwhile
    _gates(storage_hass, live=False)
    status = await controller.async_tick()
    assert status.live is False
    assert inverter.value(GRID) == "off"
    assert all(inverter.value(ce) == "off" for ce in CES)

    _charging_on(storage_hass)
    inverter.calls.clear()
    await controller.async_tick()  # still shadow: no further writes
    assert inverter.calls == []


async def test_release_on_unload_only_after_live(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state(), live=False)
    await controller.async_tick()
    _charging_on(storage_hass)
    await controller.async_release()
    assert inverter.calls == []

    _gates(storage_hass, live=True)
    await controller.async_tick()
    inverter.calls.clear()
    _charging_on(storage_hass)
    await controller.async_release()
    assert inverter.calls[0] == (GRID, False)
    assert inverter.value(GRID) == "off"


async def test_externally_switched_off_grant_goes_through_reconcile(storage_hass, inverter):
    for cap in CAPS:
        storage_hass.states.async_set(cap, "25")
    _charging_on(storage_hass)
    state = set_reconcile(activated(), "ready", None)
    controller = await _controller(storage_hass, state, live=False)
    await controller.async_tick()
    assert controller.runtime.state.active_grant.status == "active"

    storage_hass.states.async_set(GRID, "off")  # outside intervention
    status = await controller.async_tick()
    grant = controller.runtime.state.active_grant
    assert grant.status == "paused"
    assert grant.reason_code == "readback_off"
    assert status.result == "cycle"
    assert status.intents  # shadow: preparation reported, nothing written
    assert inverter.calls == []


async def test_externally_switched_off_grant_is_resumed_in_live_mode(storage_hass, inverter):
    """Current policy: an outside switch-off pauses the grant; the next
    eligible cycle prepares and enables again (the feature switch is the way
    to stop it)."""
    for cap in CAPS:
        storage_hass.states.async_set(cap, "25")
    _charging_on(storage_hass)
    controller = await _controller(storage_hass, set_reconcile(activated(), "ready", None),
                                   live=True)
    storage_hass.states.async_set(GRID, "off")
    status = await controller.async_tick()
    assert status.result == "cycle"
    assert controller.runtime.state.active_grant.status == "active"
    assert inverter.value(GRID) == "on"
    assert inverter.calls[0] == (GRID, False)  # prepared (all permissions off) first


async def test_tick_survives_an_internal_error(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state())
    with patch.object(controller.executor, "async_cycle", side_effect=RuntimeError("boom")):
        status = await controller.async_tick()
    assert status.result == "error" and "boom" in status.error
    assert (await controller.async_tick()).result == "cycle"


async def test_persistence_fault_is_retried_and_requires_reconcile(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state(), live=True)
    controller.runtime.persistence_fault = "disk"
    status = await controller.async_tick()
    assert controller.runtime.persistence_fault is None
    assert controller.runtime.state.reconcile_state.reason_code is None  # reconciled again
    assert status.result == "cycle"
