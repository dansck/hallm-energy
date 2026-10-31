"""UI configuration: one entry, entity mapping in the options."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers import selector

from .config import (
    CONF_AUDIT_TOPIC,
    CONF_COUNTER,
    CONF_COUNTER_EPOCH,
    CONF_ENABLE_MORNING_DIP,
    CONF_EV_ALLOW_VT,
    CONF_EV_CALENDAR,
    CONF_EV_CONNECTED,
    CONF_EV_DEYE_MODE,
    CONF_EV_DYNAMIC,
    CONF_EV_INTENSITY,
    CONF_EV_LIMIT,
    CONF_EV_MAX_INTENSITY,
    CONF_EV_PAUSE,
    CONF_EV_SOC,
    CONF_EV_SOC_UPDATED,
    CONF_EV_TRACKER,
    CONF_FEATURE,
    CONF_GRID_CHARGE,
    CONF_GRID_RELAY,
    CONF_GRID_VOLTAGE,
    CONF_HDO_RELAY,
    CONF_HDO_SCHEDULE,
    CONF_MANAGE_EV,
    CONF_MANAGE_FLOORS,
    CONF_NOTIFY_SERVICE,
    CONF_PHASE_CURRENTS,
    CONF_PHASE_POWERS,
    CONF_PROFILE_TOPIC,
    CONF_PV_TOPIC,
    CONF_SHADOW,
    CONF_SOC,
    NUMBER_OPTIONS,
    SLOT_COUNT,
    ConfigInvalidError,
    EnergyConfig,
    slot_key,
)
from .const import DOMAIN

_SWITCHES = ["switch", "input_boolean"]
_NUMBERS = ["number", "input_number"]


def _entity(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _schema() -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_SOC): _entity("sensor"),
        vol.Required(CONF_COUNTER): _entity("sensor"),
        vol.Required(CONF_COUNTER_EPOCH, default="1"): selector.TextSelector(),
        vol.Required(CONF_HDO_RELAY): _entity("binary_sensor"),
        vol.Required(CONF_HDO_SCHEDULE): _entity("sensor"),
        vol.Required(CONF_GRID_CHARGE): _entity(_SWITCHES),
    }
    for i in range(1, SLOT_COUNT + 1):
        fields[vol.Required(slot_key("start", i))] = _entity(_NUMBERS)
        fields[vol.Required(slot_key("capacity", i))] = _entity(_NUMBERS)
        fields[vol.Required(slot_key("charge_enable", i))] = _entity(_SWITCHES)
    fields[vol.Required(CONF_SHADOW)] = _entity(_SWITCHES)
    fields[vol.Required(CONF_FEATURE)] = _entity(_SWITCHES)
    fields[vol.Optional(CONF_PV_TOPIC)] = selector.TextSelector()
    fields[vol.Optional(CONF_AUDIT_TOPIC)] = selector.TextSelector()
    fields[vol.Optional(CONF_GRID_RELAY)] = _entity("binary_sensor")
    fields[vol.Optional(CONF_NOTIFY_SERVICE)] = selector.TextSelector()
    fields[vol.Optional(CONF_GRID_VOLTAGE)] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", multiple=True))
    for key in (CONF_PHASE_CURRENTS, CONF_PHASE_POWERS):
        fields[vol.Optional(key)] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", multiple=True))
    for key, domain in ((CONF_EV_SOC, "sensor"), (CONF_EV_CONNECTED, "binary_sensor"),
                        (CONF_EV_TRACKER, "device_tracker"), (CONF_EV_CALENDAR, "calendar"),
                        (CONF_EV_PAUSE, "switch"), (CONF_EV_INTENSITY, "number"),
                        (CONF_EV_MAX_INTENSITY, "number"), (CONF_EV_DYNAMIC, "switch"),
                        (CONF_EV_SOC_UPDATED, "sensor"), (CONF_EV_LIMIT, "number"),
                        (CONF_EV_DEYE_MODE, "select")):
        fields[vol.Optional(key)] = _entity(domain)
    fields[vol.Required(CONF_EV_ALLOW_VT, default=True)] = selector.BooleanSelector()
    fields[vol.Required(CONF_ENABLE_MORNING_DIP, default=False)] = selector.BooleanSelector()
    fields[vol.Required(CONF_MANAGE_FLOORS, default=False)] = selector.BooleanSelector()
    fields[vol.Required(CONF_MANAGE_EV, default=False)] = selector.BooleanSelector()
    fields[vol.Optional(CONF_PROFILE_TOPIC)] = selector.TextSelector()
    for key, (default, low, high) in NUMBER_OPTIONS.items():
        fields[vol.Required(key, default=default)] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=low, max=high, step="any",
                                          mode=selector.NumberSelectorMode.BOX)
        )
    return vol.Schema(fields)


def _validate(user_input: Mapping[str, Any]) -> dict[str, str]:
    try:
        EnergyConfig.from_options(user_input)
    except ConfigInvalidError:
        return {"base": "invalid_mapping"}
    return {}


class HallmEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(title="Hallm Energy", data={},
                                               options=user_input)
        schema = self.add_suggested_values_to_schema(_schema(), user_input or {})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> HallmEnergyOptionsFlow:
        return HallmEnergyOptionsFlow()


class HallmEnergyOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=user_input)
        schema = self.add_suggested_values_to_schema(
            _schema(), user_input or self.config_entry.options
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
