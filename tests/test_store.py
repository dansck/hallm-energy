"""LocalStateStore against the real `homeassistant` Store implementation
(pytest-homeassistant-custom-component's `hass` fixture provides a working
test instance with storage I/O faked onto `hass_storage`, not the real
filesystem) — this is deliberately NOT tested against a hand-written fake,
since the whole point is confirmed behaviour of the real mechanism."""

from __future__ import annotations

import pytest
from homeassistant.core import CoreState

from custom_components.hallm_energy.store import (
    LocalStateStore,
    StoreLoadError,
    StoreNotDurableError,
)


async def test_load_returns_none_when_nothing_saved_yet(hass):
    store = LocalStateStore(hass, version=1, key="hallm_energy_test_empty")
    assert await store.async_load() is None


async def test_save_then_load_round_trips(hass):
    store = LocalStateStore(hass, version=1, key="hallm_energy_test_roundtrip")
    payload = {"grant_id": "abc", "used_dc_kwh": 1.25, "window": {"start": 1, "end": 2}}
    await store.async_save(payload)
    assert await store.async_load() == payload


async def test_save_persists_immediately_without_a_separate_flush(hass, hass_storage):
    # Must be a confirmed write right away, not a debounced flush: hass_storage
    # is the fake backend Store writes to, so the data must be there right
    # after async_save without any extra flush/wait.
    store = LocalStateStore(hass, version=1, key="hallm_energy_test_immediate")
    await store.async_save({"ok": True})
    assert hass_storage["hallm_energy_test_immediate"]["data"] == {"ok": True}


async def test_save_overwrites_previous_value(hass):
    store = LocalStateStore(hass, version=1, key="hallm_energy_test_overwrite")
    await store.async_save({"n": 1})
    await store.async_save({"n": 2})
    assert await store.async_load() == {"n": 2}


async def test_uses_fsynced_atomic_writes(hass):
    # hass_storage fakes the disk layer, so the fsync path itself is not run
    # here; this pins that the wrapper asks Store for it.
    store = LocalStateStore(hass, version=1, key="hallm_energy_test_atomic")
    assert store._store._atomic_writes is True


async def test_save_while_stopping_is_reported_not_durable(hass, hass_storage):
    store = LocalStateStore(hass, version=1, key="hallm_energy_test_stopping")
    hass.set_state(CoreState.stopping)
    try:
        with pytest.raises(StoreNotDurableError):
            await store.async_save({"n": 1})
    finally:
        hass.set_state(CoreState.running)
    assert "hallm_energy_test_stopping" not in hass_storage


async def test_newer_stored_version_raises_load_error(hass, hass_storage):
    hass_storage["hallm_energy_test_newer"] = {
        "version": 5, "minor_version": 1, "key": "hallm_energy_test_newer", "data": {"x": 1}
    }
    store = LocalStateStore(hass, version=1, key="hallm_energy_test_newer")
    with pytest.raises(StoreLoadError):
        await store.async_load()


async def test_older_version_without_migration_raises_load_error(hass, hass_storage):
    hass_storage["hallm_energy_test_older"] = {
        "version": 1, "minor_version": 1, "key": "hallm_energy_test_older", "data": {"x": 1}
    }
    store = LocalStateStore(hass, version=2, key="hallm_energy_test_older")
    with pytest.raises(StoreLoadError):
        await store.async_load()
    assert hass_storage["hallm_energy_test_older"]["version"] == 1  # not rewritten


async def test_different_keys_do_not_collide(hass):
    store_a = LocalStateStore(hass, version=1, key="hallm_energy_test_a")
    store_b = LocalStateStore(hass, version=1, key="hallm_energy_test_b")
    await store_a.async_save({"who": "a"})
    await store_b.async_save({"who": "b"})
    assert await store_a.async_load() == {"who": "a"}
    assert await store_b.async_load() == {"who": "b"}
