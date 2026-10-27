"""LocalStateStore against the real `homeassistant` Store implementation
(pytest-homeassistant-custom-component's `hass` fixture provides a working
test instance with storage I/O faked onto `hass_storage`, not the real
filesystem) — this is deliberately NOT tested against a hand-written fake,
since the whole point is confirmed behaviour of the real mechanism."""

from __future__ import annotations

from custom_components.hallm_energy.store import LocalStateStore


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


async def test_different_keys_do_not_collide(hass):
    store_a = LocalStateStore(hass, version=1, key="hallm_energy_test_a")
    store_b = LocalStateStore(hass, version=1, key="hallm_energy_test_b")
    await store_a.async_save({"who": "a"})
    await store_b.async_save({"who": "b"})
    assert await store_a.async_load() == {"who": "a"}
    assert await store_b.async_load() == {"who": "b"}
