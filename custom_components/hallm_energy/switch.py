"""The switch that blocks grid charging: on = no grant is prepared, started or kept, until
a person switches it off. It also turns itself on when charging was switched off from
outside during a grant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .data import HallmEnergyData


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([GridChargingBlockSwitch(entry)])


class GridChargingBlockSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "grid_charging_block"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._data: HallmEnergyData = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_grid_charging_block"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Hallm Energy",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        if self._data.controller is not None:
            self.async_on_remove(self._data.controller.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self._data.controller is not None and self._data.runtime.state is not None

    @property
    def is_on(self) -> bool:
        state = self._data.runtime.state
        return state is not None and state.manual_block is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._data.runtime.state
        block = None if state is None else state.manual_block
        return {} if block is None else {"reason": block.reason, "since": block.since.isoformat()}

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._data.controller is not None:
            await self._data.controller.async_set_manual_block(True, "user")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._data.controller is not None:
            await self._data.controller.async_set_manual_block(False, "user")
