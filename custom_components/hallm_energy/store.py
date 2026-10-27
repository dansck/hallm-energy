"""Durable local state file with confirmed writes.

Stored under Home Assistant's `.storage/` directory in the same envelope
format Home Assistant uses (`version`, `minor_version`, `key`, `data`), but
written and read by this module directly instead of through
`homeassistant.helpers.storage.Store`, because Store does not let a caller
know whether a write succeeded: it logs write errors and returns normally,
and it renames a corrupt file aside and then reports "nothing saved".

Guarantees callers can rely on before acting on a saved state:

* `async_save` returns only after the file was written with fsync +
  atomic rename (`write_utf8_file_atomic`) AND read back from disk equal to
  what was requested. Any failure raises `StoreNotDurableError`; the
  previous file content stays in place.
* `async_load` returns `None` only when the file does not exist. An
  unreadable/corrupt file, an unexpected envelope or a version other than
  the one this code understands raises `StoreLoadError` and the file is left
  untouched for diagnosis.

No business schema here — callers own the dict they load/save.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.file import WriteError, write_utf8_file_atomic

STORAGE_DIR = ".storage"
MINOR_VERSION = 1


class StoreLoadError(Exception):
    """Stored data exists but cannot be used; keep it untouched for diagnosis."""


class StoreNotDurableError(Exception):
    """The write was not confirmed on disk; nothing may rely on it."""


class LocalStateStore:
    def __init__(
        self, hass: HomeAssistant, *, version: int, key: str, path: str | None = None
    ) -> None:
        self._hass = hass
        self.version = version
        self.key = key
        self.path = path or hass.config.path(STORAGE_DIR, key)
        self._lock = asyncio.Lock()

    async def async_load(self) -> dict[str, Any] | None:
        async with self._lock:
            return await self._hass.async_add_executor_job(self._load)

    async def async_save(self, data: dict[str, Any]) -> None:
        envelope = {
            "version": self.version,
            "minor_version": MINOR_VERSION,
            "key": self.key,
            "data": data,
        }
        try:
            payload = json.dumps(envelope, ensure_ascii=False, allow_nan=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise StoreNotDurableError(f"state is not serializable: {exc}") from exc
        async with self._lock:
            await self._hass.async_add_executor_job(self._write_and_verify, payload, envelope)

    def _load(self) -> dict[str, Any] | None:
        if not os.path.exists(self.path):
            return None
        try:
            raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreLoadError(f"stored state cannot be read: {exc}") from exc
        if not isinstance(raw, dict) or "version" not in raw or "data" not in raw:
            raise StoreLoadError("stored state has an unexpected envelope")
        if raw.get("key") != self.key:
            raise StoreLoadError(f"stored key {raw.get('key')!r} != {self.key!r}")
        if raw["version"] != self.version or raw.get("minor_version", 1) != MINOR_VERSION:
            raise StoreLoadError(
                f"stored version {raw['version']}.{raw.get('minor_version', 1)} is not "
                f"{self.version}.{MINOR_VERSION} and no migration exists"
            )
        if not isinstance(raw["data"], dict):
            raise StoreLoadError("stored data is not a mapping")
        return raw["data"]

    def _write_and_verify(self, payload: str, envelope: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            write_utf8_file_atomic(self.path, payload, private=True)
            on_disk = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (WriteError, OSError, ValueError) as exc:
            raise StoreNotDurableError(f"state write failed: {exc}") from exc
        if on_disk != envelope:
            raise StoreNotDurableError("state read back from disk differs from what was written")
