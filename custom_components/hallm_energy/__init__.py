"""Hallm Energy — manages grid-charging grants for a home battery via a
single local authority.

**SKELETON** — proves installability via HACS, the integration lifecycle
(setup/unload), and real local persistence via `LocalStateStore` (see
`store.py`) instead of mirroring state into an `input_text` entity. Grant
decision logic, restart reconcile, safety limits and services are not
implemented yet — `state` loaded here is only stashed for a later step to
use, nothing currently reads or acts on it."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .store import LocalStateStore

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    store = LocalStateStore(hass, version=STORAGE_VERSION, key=STORAGE_KEY)
    state = await store.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"store": store, "state": state}
    _LOGGER.info(
        "%s: setup — local state %s", DOMAIN, "loaded" if state is not None else "empty (first run)"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
