"""LocalStateStore against the real filesystem (temp config dir): confirmed
writes, read-back verification, untouched unusable files, real I/O failures."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.util.file import WriteError

from custom_components.hallm_energy import store as store_module
from custom_components.hallm_energy.store import (
    LocalStateStore,
    StoreLoadError,
    StoreNotDurableError,
)

KEY = "hallm_energy_test"


def _store(hass, version: int = 1) -> LocalStateStore:
    return LocalStateStore(hass, version=version, key=KEY)


def _write_raw(hass, content: str) -> Path:
    path = Path(hass.config.path(".storage", KEY))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def test_load_returns_none_when_file_missing(storage_hass):
    assert await _store(storage_hass).async_load() is None


async def test_save_writes_file_and_load_reads_it_back(storage_hass):
    store = _store(storage_hass)
    payload = {"grant_id": "abc", "used_dc_kwh": 3.2256000000000005, "window": {"start": 1}}
    await store.async_save(payload)
    on_disk = json.loads(Path(store.path).read_text(encoding="utf-8"))
    assert on_disk == {"version": 1, "minor_version": 1, "key": KEY, "data": payload}
    assert await _store(storage_hass).async_load() == payload  # fresh instance, no cache


async def test_saved_file_is_private(storage_hass):
    store = _store(storage_hass)
    await store.async_save({"n": 1})
    assert stat.S_IMODE(os.stat(store.path).st_mode) & 0o077 == 0


async def test_uses_fsynced_atomic_writer(storage_hass):
    store = _store(storage_hass)
    with patch.object(
        store_module, "write_utf8_file_atomic", wraps=store_module.write_utf8_file_atomic
    ) as writer:
        await store.async_save({"n": 1})
    assert writer.call_count == 1


async def test_write_error_is_raised_and_previous_revision_kept(storage_hass):
    store = _store(storage_hass)
    await store.async_save({"revision": 1})
    with patch.object(
        store_module, "write_utf8_file_atomic", side_effect=WriteError("simulated disk failure")
    ), pytest.raises(StoreNotDurableError):
        await store.async_save({"revision": 2})
    assert await _store(storage_hass).async_load() == {"revision": 1}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_real_filesystem_failure_is_raised(storage_hass):
    store = _store(storage_hass)
    await store.async_save({"revision": 1})
    storage_dir = Path(store.path).parent
    storage_dir.chmod(0o500)  # rename into a read-only directory fails for real
    try:
        with pytest.raises(StoreNotDurableError):
            await store.async_save({"revision": 2})
    finally:
        storage_dir.chmod(0o700)
    assert await _store(storage_hass).async_load() == {"revision": 1}


async def test_read_back_mismatch_is_not_durable(storage_hass):
    store = _store(storage_hass)
    real_write = store_module.write_utf8_file_atomic

    def write_something_else(path, data, private=False, mode="w"):
        real_write(path, data.replace('"n": 2', '"n": 3'), private=private, mode=mode)

    with patch.object(store_module, "write_utf8_file_atomic", side_effect=write_something_else):
        with pytest.raises(StoreNotDurableError):
            await store.async_save({"n": 2})


async def test_non_serializable_state_is_not_durable(storage_hass):
    with pytest.raises(StoreNotDurableError):
        await _store(storage_hass).async_save({"x": float("nan")})


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 1, "key": KEY}),
        json.dumps({"version": 1, "minor_version": 1, "key": "other", "data": {}}),
        json.dumps({"version": 1, "minor_version": 1, "key": KEY, "data": []}),
    ],
)
async def test_unusable_file_raises_and_is_left_untouched(storage_hass, content):
    path = _write_raw(storage_hass, content)
    with pytest.raises(StoreLoadError):
        await _store(storage_hass).async_load()
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("stored_version,code_version", [(2, 1), (1, 2)])
async def test_version_mismatch_raises_and_is_left_untouched(
    storage_hass, stored_version, code_version
):
    content = json.dumps(
        {"version": stored_version, "minor_version": 1, "key": KEY, "data": {"x": 1}}
    )
    path = _write_raw(storage_hass, content)
    with pytest.raises(StoreLoadError):
        await _store(storage_hass, version=code_version).async_load()
    assert path.read_text(encoding="utf-8") == content


async def test_ha_store_swallows_write_errors_which_is_why_it_is_not_used(storage_hass):
    # Characterization of homeassistant.helpers.storage.Store: a failing write
    # is only logged and async_save returns normally. If this ever starts
    # raising, Store could be reconsidered.
    from unittest.mock import AsyncMock

    from homeassistant.helpers.storage import Store

    ha_store = Store(storage_hass, 1, "hallm_energy_characterization", atomic_writes=True)
    with patch.object(
        ha_store, "_async_write_data", AsyncMock(side_effect=WriteError("simulated disk failure"))
    ):
        await ha_store.async_save({"n": 1})  # no exception
