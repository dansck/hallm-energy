"""Integration startup against a real (test) Home Assistant: first run,
restart, lost/invalid/foreign state, singleton. Shadow only — no device writes."""

from __future__ import annotations

import copy

import pytest
from homeassistant.exceptions import ConfigEntryError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hallm_energy import async_setup_entry, async_unload_entry
from custom_components.hallm_energy.const import (
    CONF_INSTALLATION_ID,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from custom_components.hallm_energy.lifecycle import new_installation_state, set_reconcile
from custom_components.hallm_energy.runtime import RuntimeBlockedError
from custom_components.hallm_energy.state_schema import parse_store_state


def _entry(hass, **data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    return entry


def _stored(hass_storage, data: dict, version: int = STORAGE_VERSION) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": version,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": data,
    }


def _state(installation_id: str = "inst-1"):
    return new_installation_state(
        installation_id=installation_id, policy_version="p", core_version="c"
    )


async def test_first_run_creates_uninitialized_state_and_marker(hass, hass_storage):
    entry = _entry(hass)
    assert await async_setup_entry(hass, entry) is True
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.blocked_reason is None
    assert runtime.state.provisioning_state == "uninitialized"
    assert runtime.may_enable is False
    assert entry.data[CONF_INSTALLATION_ID] == runtime.state.installation_id
    assert parse_store_state(hass_storage[STORAGE_KEY]["data"]) == runtime.state


async def test_restart_requires_reconcile_even_if_stored_ready(hass, hass_storage):
    stored = set_reconcile(_state(), "ready", None)
    _stored(hass_storage, stored.to_dict())
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await async_setup_entry(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.state.reconcile_state.value == "required"
    assert runtime.state.reconcile_state.reason_code == "startup"
    assert hass_storage[STORAGE_KEY]["data"]["reconcile_state"]["value"] == "required"


async def test_marker_without_store_is_lost_state_not_first_run(hass, hass_storage):
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await async_setup_entry(hass, entry)
    state = hass.data[DOMAIN][entry.entry_id].state
    assert state.installation_id == "inst-1"
    assert state.provisioning_state == "blocked"
    assert state.reconcile_state.value == "blocked"
    assert state.reconcile_state.reason_code == "store_lost"
    assert state.active_grant is None


async def test_invalid_stored_state_blocks_and_is_left_untouched(hass, hass_storage):
    bad = _state().to_dict()
    bad["provisioning_state"] = "whatever"
    _stored(hass_storage, bad)
    before = copy.deepcopy(hass_storage[STORAGE_KEY])
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await async_setup_entry(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.blocked_reason == "store_invalid"
    assert runtime.state is None
    assert hass_storage[STORAGE_KEY] == before
    with pytest.raises(RuntimeBlockedError):
        await runtime.async_commit(_state())
    assert hass_storage[STORAGE_KEY] == before


async def test_newer_storage_version_blocks_and_is_left_untouched(hass, hass_storage):
    _stored(hass_storage, _state().to_dict(), version=STORAGE_VERSION + 1)
    before = copy.deepcopy(hass_storage[STORAGE_KEY])
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await async_setup_entry(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id].blocked_reason == "store_unusable"
    assert hass_storage[STORAGE_KEY] == before


async def test_foreign_installation_state_blocks(hass, hass_storage):
    _stored(hass_storage, _state("inst-other").to_dict())
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await async_setup_entry(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id].blocked_reason == "installation_mismatch"
    assert entry.data[CONF_INSTALLATION_ID] == "inst-1"


async def test_valid_state_without_marker_is_adopted(hass, hass_storage):
    # crash after the Store write but before the marker was recorded
    _stored(hass_storage, _state("inst-7").to_dict())
    entry = _entry(hass)
    await async_setup_entry(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.blocked_reason is None
    assert entry.data[CONF_INSTALLATION_ID] == "inst-7"


async def test_blocked_state_stays_blocked_across_restart(hass, hass_storage):
    blocked = set_reconcile(_state(), "blocked", "counter_epoch_changed")
    _stored(hass_storage, blocked.to_dict())
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await async_setup_entry(hass, entry)
    state = hass.data[DOMAIN][entry.entry_id].state
    assert state.reconcile_state.value == "blocked"
    assert state.reconcile_state.reason_code == "counter_epoch_changed"


async def test_second_entry_is_refused(hass, hass_storage):
    await async_setup_entry(hass, _entry(hass))
    with pytest.raises(ConfigEntryError):
        await async_setup_entry(hass, _entry(hass))


async def test_unload_removes_runtime(hass, hass_storage):
    entry = _entry(hass)
    await async_setup_entry(hass, entry)
    assert await async_unload_entry(hass, entry) is True
    assert entry.entry_id not in hass.data[DOMAIN]
