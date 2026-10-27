"""Confirmed-write wrapper around Home Assistant's own local `Store`
(homeassistant.helpers.storage) — replaces mirroring state into an
`input_text` entity, which has a 255-character limit no non-trivial grant
payload reliably fits under.

Guarantees callers can rely on before acting on a saved state:

* Writes use `atomic_writes=True` (temp file + fsync + rename). The default
  Store write replaces the file atomically but without fsync, so a power
  loss right after "saved" could still lose it.
* `async_save` raises `StoreNotDurableError` instead of returning when the
  write is not confirmed on disk — including while Home Assistant is
  stopping, where Store would only schedule the write for later.
* The debounced `Store.async_delay_save` is never used.
* Stored data that cannot be used (newer or unknown version, failed
  migration, unreadable file) raises `StoreLoadError`. The caller must then
  block and must NOT overwrite it.

No business schema here — callers own the dict they load/save.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError


class StoreLoadError(Exception):
    """Stored data exists but cannot be used; keep it untouched for diagnosis."""


class StoreNotDurableError(Exception):
    """The write was not confirmed on disk; nothing may rely on it."""


class LocalStateStore:
    """Thin wrapper so callers don't reach into `homeassistant.helpers.storage`
    directly and can't reach for the debounced save path."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        version: int,
        key: str,
        minor_version: int = 1,
    ) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, version, key, minor_version=minor_version, atomic_writes=True
        )

    async def async_load(self) -> dict[str, Any] | None:
        """`None` means "nothing saved under this key" — the caller decides
        whether that is a first run or a lost state."""
        try:
            return await self._store.async_load()
        except NotImplementedError as exc:  # older major version, no migration
            raise StoreLoadError("stored state version needs a missing migration") from exc
        except HomeAssistantError as exc:  # newer version, unreadable/corrupt file
            raise StoreLoadError(f"stored state cannot be loaded: {exc}") from exc

    async def async_save(self, data: dict[str, Any]) -> None:
        if self._hass.state is CoreState.stopping:
            raise StoreNotDurableError("Home Assistant is stopping; the write would be deferred")
        try:
            await self._store.async_save(data)
        except (WriteError, OSError) as exc:
            raise StoreNotDurableError(f"state write failed: {exc}") from exc
