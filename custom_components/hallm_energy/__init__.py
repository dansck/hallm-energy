"""Hallm Energy — manages grid-charging grants for a home battery via a
single local authority.

Current scope: lifecycle, validated local state (`state_schema.py`,
`lifecycle.py`) persisted through `LocalStateStore`, startup reconcile gate.
Shadow only — nothing here writes to any device yet.

Only one installation is supported: the state lives under one global storage
key, so a second config entry is refused rather than sharing that file."""

from __future__ import annotations

import logging
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import CONF_INSTALLATION_ID, DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .runtime import EnergyRuntime
from .store import LocalStateStore

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if any(entry_id != entry.entry_id for entry_id in domain_data):
        raise ConfigEntryError("only one Hallm Energy installation is supported")

    runtime = EnergyRuntime(
        hass, LocalStateStore(hass, version=STORAGE_VERSION, key=STORAGE_KEY)
    )
    installation_id = await runtime.async_start(
        known_installation_id=entry.data.get(CONF_INSTALLATION_ID),
        new_installation_id=lambda: uuid4().hex,
    )
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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
