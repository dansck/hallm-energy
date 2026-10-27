"""UI configuration: one entry, entity mapping in the options."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers import selector

from .config import (
    CONF_COUNTER,
    CONF_COUNTER_EPOCH,
    CONF_FEATURE,
    CONF_GRID_CHARGE,
    CONF_HDO_RELAY,
    CONF_HDO_SCHEDULE,
    CONF_SHADOW,
    CONF_SOC,
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
