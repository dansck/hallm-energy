"""Hallm Energy — manages grid-charging grants for a home battery via a
single local authority.

Setup loads and validates the stored state (`runtime.py`), then — when the
entity mapping is configured — starts the periodic control loop
(`controller.py`). Device writes happen only in live mode (see `executor.py`).

Only one installation is supported: the state lives under one global storage
key, so a second config entry is refused rather than sharing that file."""

from __future__ import annotations

import logging
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service

from .config import ConfigInvalidError, EnergyConfig
from .const import (
    CONF_INSTALLATION_ID,
    DOMAIN,
    SERVICE_PROVISION,
    SERVICE_SAFETY_STOP,
    STORAGE_KEY,
    STORAGE_VERSION,
    TICK_INTERVAL,
)
from .controller import EnergyController, ProvisionError
from .data import HallmEnergyData
from .runtime import EnergyRuntime
from .store import LocalStateStore, StoreNotDurableError

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if any(entry_id != entry.entry_id for entry_id in domain_data):
        raise ConfigEntryError("only one Hallm Energy installation is supported")

    runtime = EnergyRuntime(
        hass, LocalStateStore(hass, version=STORAGE_VERSION, key=STORAGE_KEY)
    )
    try:
        installation_id = await runtime.async_start(
            known_installation_id=entry.data.get(CONF_INSTALLATION_ID),
            new_installation_id=lambda: uuid4().hex,
        )
    except StoreNotDurableError as exc:
        raise ConfigEntryNotReady(f"state could not be persisted: {exc}") from exc
    # Marker after the Store write: a crash in between leaves a valid state
    # without a marker, which the next start adopts.
    if installation_id is not None and entry.data.get(CONF_INSTALLATION_ID) != installation_id:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_INSTALLATION_ID: installation_id}
        )
    domain_data[entry.entry_id] = runtime
    if runtime.blocked_reason is not None:
        _LOGGER.error(
            "%s: blocked (%s); stored state left untouched", DOMAIN, runtime.blocked_reason
        )

    controller, config_error = None, None
    try:
        config = EnergyConfig.from_options(entry.options)
    except ConfigInvalidError as exc:
        config, config_error = None, str(exc)
        _LOGGER.error("%s: entity mapping invalid (%s); control loop not started", DOMAIN, exc)
    if config is not None:
        controller = EnergyController(hass, runtime, config)
    entry.runtime_data = HallmEnergyData(runtime, controller, config_error)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if controller is not None:
        _start_loop(hass, entry, controller)
    _register_services(hass)
    return True


def _start_loop(hass: HomeAssistant, entry: ConfigEntry, controller: EnergyController) -> None:
    async def tick(_now=None) -> None:
        await controller.async_tick()

    @callback
    def gates_changed(_event: Event) -> None:
        # leaving live mode must stop promptly, not on the next interval
        entry.async_create_background_task(hass, tick(), f"{DOMAIN} gate change")

    entry.async_on_unload(async_track_time_interval(hass, tick, TICK_INTERVAL))
    entry.async_on_unload(async_track_state_change_event(
        hass, [controller.config.shadow_entity, controller.config.feature_entity], gates_changed
    ))
    entry.async_create_background_task(hass, tick(), f"{DOMAIN} first tick")


def _controller(hass: HomeAssistant) -> EnergyController:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        data: HallmEnergyData = entry.runtime_data
        if data.controller is not None:
            return data.controller
    raise ServiceValidationError("Hallm Energy is not configured")


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_PROVISION):
        return

    async def provision(call: ServiceCall) -> None:
        controller = _controller(hass)
        try:
            await controller.async_provision()
        except (ProvisionError, StoreNotDurableError) as exc:
            raise ServiceValidationError(str(exc)) from exc
        await controller.async_tick()

    async def safety_stop(call: ServiceCall) -> None:
        await _controller(hass).async_safety_stop()

    async_register_admin_service(hass, DOMAIN, SERVICE_PROVISION, provision)
    async_register_admin_service(hass, DOMAIN, SERVICE_SAFETY_STOP, safety_stop)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data: HallmEnergyData | None = getattr(entry, "runtime_data", None)
    if data is not None and data.controller is not None:
        await data.controller.async_release()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        for service in (SERVICE_PROVISION, SERVICE_SAFETY_STOP):
            hass.services.async_remove(DOMAIN, service)
    return unloaded
