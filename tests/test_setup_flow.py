"""Config/options flow and the full entry setup with the control loop."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hallm_energy.const import DOMAIN
from tests.test_executor import FEATURE, SHADOW, FakeInverter
from tests.test_inputs import NOW, OPTIONS, set_telemetry

STATUS = "sensor.hallm_energy_status"


@pytest.fixture
def inverter(storage_hass, freezer):
    freezer.move_to(NOW)
    inverter = FakeInverter(storage_hass)
    set_telemetry(storage_hass)
    storage_hass.states.async_set(SHADOW, "on")
    storage_hass.states.async_set(FEATURE, "on")
    return inverter


async def _settle(hass) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_user_flow_creates_entry_with_mapping_in_options(storage_hass, inverter):
    hass = storage_hass
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], OPTIONS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    from custom_components.hallm_energy.config import NUMBER_OPTIONS

    defaults = {key: spec[0] for key, spec in NUMBER_OPTIONS.items()}
    assert dict(entry.options) == {**OPTIONS, **defaults}
    await _settle(hass)
    assert hass.states.get(STATUS).state == "not_provisioned"
    assert hass.states.get(STATUS).attributes["live"] is False


async def test_user_flow_rejects_entity_in_two_written_roles(storage_hass, inverter):
    hass = storage_hass
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    bad = {**OPTIONS, "slot2_charge_enable_entity": OPTIONS["grid_charge_entity"]}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_mapping"}


async def test_second_flow_is_aborted(storage_hass, inverter):
    hass = storage_hass
    MockConfigEntry(domain=DOMAIN, options=OPTIONS).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow_changes_mapping_and_reloads(storage_hass, inverter):
    hass = storage_hass
    entry = MockConfigEntry(domain=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, "counter_epoch": "e2"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass)
    assert entry.options["counter_epoch"] == "e2"
    assert entry.runtime_data.controller.config.counter_epoch == "e2"


async def test_unconfigured_entry_keeps_state_and_runs_no_loop(storage_hass, inverter):
    hass = storage_hass
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    assert entry.runtime_data.controller is None
    assert hass.states.get(STATUS).state == "not_configured"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "provision", blocking=True)


async def test_invalid_mapping_is_reported_not_run(storage_hass, inverter):
    hass = storage_hass
    entry = MockConfigEntry(domain=DOMAIN, options={**OPTIONS, "soc_entity": ""})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    assert entry.runtime_data.controller is None
    assert "soc_entity" in hass.states.get(STATUS).attributes["config_error"]


async def test_provision_service_then_idle(storage_hass, inverter):
    hass = storage_hass
    hass.states.async_set("sensor.bms_soc", "50.0")  # above the reserve: nothing to recover
    entry = MockConfigEntry(domain=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    await hass.services.async_call(DOMAIN, "provision", blocking=True)
    await _settle(hass)
    assert hass.states.get(STATUS).state == "idle"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "provision", blocking=True)
    assert inverter.calls == []


async def test_unload_removes_services(storage_hass, inverter):
    hass = storage_hass
    entry = MockConfigEntry(domain=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, "provision")


# The MQTT client keeps a periodic housekeeping timer; with the frozen clock it
# outlives the test (HA test harness check), it is not ours.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_house_load_profile_arrives_over_mqtt(storage_hass, inverter, mqtt_mock):
    import json

    from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

    from tests.test_recovery_flow import _profile

    hass = storage_hass
    entry = MockConfigEntry(domain=DOMAIN, options={**OPTIONS,
                                                    "house_load_profile_topic": "test/profile",
                                                    "pv_forecast_topic": "test/pv"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    profile = _profile(now=NOW)
    async_fire_mqtt_message(hass, "test/profile", json.dumps(profile.to_dict()), retain=True)
    await _settle(hass)
    assert entry.runtime_data.controller.profiles.profile == profile
    from tests.test_recovery_flow import _forecast, _sunny

    forecast = _forecast(_sunny, now=NOW)
    async_fire_mqtt_message(hass, "test/pv", json.dumps(forecast.to_dict()), retain=True)
    await _settle(hass)
    assert entry.runtime_data.controller.pv_forecasts.forecast == forecast
    async_fire_mqtt_message(hass, "test/profile", "not json")
    await _settle(hass)
    assert entry.runtime_data.controller.profiles.error.startswith("invalid")
    assert entry.runtime_data.controller.profiles.profile == profile
