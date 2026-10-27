"""Integration startup against a real (test) Home Assistant: first run,
restart, lost/invalid/foreign state, singleton. Shadow only — no device writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

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


async def _setup(hass, entry) -> bool:
    return await hass.config_entries.async_setup(entry.entry_id)


def _path(hass) -> Path:
    return Path(hass.config.path(".storage", STORAGE_KEY))


def _stored(hass, data: dict, version: int = STORAGE_VERSION) -> None:
    path = _path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": version, "minor_version": 1, "key": STORAGE_KEY, "data": data}),
        encoding="utf-8",
    )


def _on_disk(hass) -> dict:
    return json.loads(_path(hass).read_text(encoding="utf-8"))


def _state(installation_id: str = "inst-1"):
    return new_installation_state(
        installation_id=installation_id, policy_version="p", core_version="c"
    )


async def test_first_run_creates_uninitialized_state_and_marker(storage_hass):
    hass = storage_hass
    entry = _entry(hass)
    assert await _setup(hass, entry) is True
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.blocked_reason is None
    assert runtime.state.provisioning_state == "uninitialized"
    assert runtime.may_enable is False
    assert entry.data[CONF_INSTALLATION_ID] == runtime.state.installation_id
    assert parse_store_state(_on_disk(hass)["data"]) == runtime.state


async def test_restart_requires_reconcile_even_if_stored_ready(storage_hass):
    hass = storage_hass
    stored = set_reconcile(_state(), "ready", None)
    _stored(hass, stored.to_dict())
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await _setup(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.state.reconcile_state.value == "required"
    assert runtime.state.reconcile_state.reason_code == "startup"
    assert _on_disk(hass)["data"]["reconcile_state"]["value"] == "required"


async def test_marker_without_store_is_lost_state_not_first_run(storage_hass):
    hass = storage_hass
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await _setup(hass, entry)
    state = hass.data[DOMAIN][entry.entry_id].state
    assert state.installation_id == "inst-1"
    assert state.provisioning_state == "blocked"
    assert state.reconcile_state.value == "blocked"
    assert state.reconcile_state.reason_code == "store_lost"
    assert state.active_grant is None


async def test_invalid_stored_state_blocks_and_is_left_untouched(storage_hass):
    hass = storage_hass
    bad = _state().to_dict()
    bad["provisioning_state"] = "whatever"
    _stored(hass, bad)
    before = _path(hass).read_text()
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await _setup(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.blocked_reason == "store_invalid"
    assert runtime.state is None
    assert _path(hass).read_text() == before
    with pytest.raises(RuntimeBlockedError):
        await runtime.async_commit(_state())
    assert _path(hass).read_text() == before


async def test_newer_storage_version_blocks_and_is_left_untouched(storage_hass):
    hass = storage_hass
    _stored(hass, _state().to_dict(), version=STORAGE_VERSION + 1)
    before = _path(hass).read_text()
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await _setup(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id].blocked_reason == "store_unusable"
    assert _path(hass).read_text() == before


async def test_foreign_installation_state_blocks(storage_hass):
    hass = storage_hass
    _stored(hass, _state("inst-other").to_dict())
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await _setup(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id].blocked_reason == "installation_mismatch"
    assert entry.data[CONF_INSTALLATION_ID] == "inst-1"


async def test_valid_state_without_marker_is_adopted(storage_hass):
    hass = storage_hass
    # crash after the Store write but before the marker was recorded
    _stored(hass, _state("inst-7").to_dict())
    entry = _entry(hass)
    await _setup(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.blocked_reason is None
    assert entry.data[CONF_INSTALLATION_ID] == "inst-7"


async def test_blocked_state_stays_blocked_across_restart(storage_hass):
    hass = storage_hass
    blocked = set_reconcile(_state(), "blocked", "counter_epoch_changed")
    _stored(hass, blocked.to_dict())
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await _setup(hass, entry)
    state = hass.data[DOMAIN][entry.entry_id].state
    assert state.reconcile_state.value == "blocked"
    assert state.reconcile_state.reason_code == "counter_epoch_changed"


async def test_second_entry_is_refused(storage_hass):
    hass = storage_hass
    await _setup(hass, _entry(hass))
    second = _entry(hass)
    assert await _setup(hass, second) is False
    assert second.state is ConfigEntryState.SETUP_ERROR


async def test_unload_removes_runtime(storage_hass):
    hass = storage_hass
    entry = _entry(hass)
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id) is True
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_corrupt_file_blocks_and_is_not_moved_aside(storage_hass):
    hass = storage_hass
    path = _path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{truncated", encoding="utf-8")
    entry = _entry(hass, **{CONF_INSTALLATION_ID: "inst-1"})
    await _setup(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id].blocked_reason == "store_unusable"
    assert path.read_text(encoding="utf-8") == "{truncated"


async def test_startup_write_failure_is_not_ready(storage_hass):
    from unittest.mock import patch

    from homeassistant.util.file import WriteError

    from custom_components.hallm_energy import store as store_module

    hass = storage_hass
    with patch.object(store_module, "write_utf8_file_atomic", side_effect=WriteError("disk")):
        entry = _entry(hass)
        assert await _setup(hass, entry) is False
    assert entry.state is ConfigEntryState.SETUP_RETRY
