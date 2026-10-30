"""The owner's answer to "do you drive to work tomorrow?": the one thing the plan cannot know.
Set by the assistant after it has asked; it never answers by itself and expires at midnight."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .data import HallmEnergyData

OPTIONS = ["unknown", "yes", "no"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    controller = entry.runtime_data.controller
    if controller is not None and controller.ev.enabled:
        async_add_entities([EvDriveTomorrowSelect(entry)])


class EvDriveTomorrowSelect(RestoreEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "ev_drive_tomorrow"
    _attr_should_poll = False
    _attr_options = OPTIONS

    def __init__(self, entry: ConfigEntry) -> None:
        self._data: HallmEnergyData = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_ev_drive_tomorrow"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Hallm Energy",
            entry_type=DeviceEntryType.SERVICE)

    async def async_added_to_hass(self) -> None:
        controller = self._data.controller
        if controller is None:
            return
        last = await self.async_get_last_state()
        if last is not None:
            controller.restore_ev_drive_tomorrow(last.state, last.last_changed)
        self.async_on_remove(controller.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self._data.controller is not None

    @property
    def current_option(self) -> str:
        controller = self._data.controller
        return "unknown" if controller is None else controller.drive_answer(dt_util.utcnow())

    async def async_select_option(self, option: str) -> None:
        if self._data.controller is not None:
            await self._data.controller.async_set_ev_drive_tomorrow(option)
