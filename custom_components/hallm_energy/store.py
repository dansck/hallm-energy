"""Generic confirmed-write wrapper around Home Assistant's own local `Store`
(homeassistant.helpers.storage) — this is what replaces mirroring state into
an `input_text` entity, which has a 255-character limit no non-trivial grant
payload reliably fits under.

Always writes with an immediate, confirmed save (`Store.async_save`), never
the debounced `Store.async_delay_save`: a crash between a decision (e.g.
"allow charging") and a delayed flush would leave that decision unrecorded
on disk while it had already been acted on physically, which is exactly the
failure mode this exists to prevent.

This wrapper itself carries no business-specific schema (no "grant" shape
here) — callers own the dict they load/save. Schema/version/migration for
a specific stored shape is a decision for the caller, not this module.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store


class LocalStateStore:
    """Thin wrapper so callers don't reach into `homeassistant.helpers.storage`
    directly and don't accidentally reach for the debounced save path."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        version: int,
        key: str,
        minor_version: int = 1,
    ) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, version, key, minor_version=minor_version
        )

    async def async_load(self) -> dict[str, Any] | None:
        """`None` means "nothing saved yet" — a normal, expected state on
        first run, not an error."""
        return await self._store.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        await self._store.async_save(data)
