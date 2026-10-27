"""async_setup_entry/async_unload_entry — skeleton lifecycle plus real local
persistence wiring. No grant decision logic here yet; this only checks that
setup loads/stashes state and unload cleans up."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.hallm_energy import async_setup_entry, async_unload_entry
from custom_components.hallm_energy.const import DOMAIN, STORAGE_KEY
from custom_components.hallm_energy.store import LocalStateStore


def _fake_entry(entry_id: str = "entry1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


async def test_setup_entry_loads_empty_state_on_first_run(hass):
    entry = _fake_entry()
    assert await async_setup_entry(hass, entry) is True
    assert hass.data[DOMAIN][entry.entry_id]["state"] is None
    assert isinstance(hass.data[DOMAIN][entry.entry_id]["store"], LocalStateStore)


async def test_setup_entry_loads_previously_saved_state(hass):
    # simulates an HA restart: state was saved earlier under the same key
    store = LocalStateStore(hass, version=1, key=STORAGE_KEY)
    await store.async_save({"example": "restored"})

    entry = _fake_entry()
    await async_setup_entry(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id]["state"] == {"example": "restored"}


async def test_unload_entry_removes_entry_data(hass):
    entry = _fake_entry()
    await async_setup_entry(hass, entry)
    assert entry.entry_id in hass.data[DOMAIN]

    assert await async_unload_entry(hass, entry) is True
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_two_entries_do_not_collide(hass):
    entry_a, entry_b = _fake_entry("a"), _fake_entry("b")
    await async_setup_entry(hass, entry_a)
    await async_setup_entry(hass, entry_b)
    assert set(hass.data[DOMAIN]) == {"a", "b"}
