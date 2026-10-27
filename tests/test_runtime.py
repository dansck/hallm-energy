"""EnergyRuntime persistence fault handling with a ready state."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.util.file import WriteError

from custom_components.hallm_energy import store as store_module
from custom_components.hallm_energy.const import STORAGE_KEY
from custom_components.hallm_energy.lifecycle import set_reconcile
from custom_components.hallm_energy.runtime import EnergyRuntime
from custom_components.hallm_energy.store import LocalStateStore, StoreNotDurableError
from tests.test_lifecycle import activated, step


async def _ready_runtime(hass) -> EnergyRuntime:
    runtime = EnergyRuntime(hass, LocalStateStore(hass, version=1, key=STORAGE_KEY))
    await runtime.async_commit(set_reconcile(activated(), "ready", None))
    assert runtime.may_enable is True
    return runtime


def _failing():
    return patch.object(store_module, "write_utf8_file_atomic", side_effect=WriteError("disk"))


async def test_failed_commit_blocks_enabling_and_keeps_old_state(storage_hass):
    runtime = await _ready_runtime(storage_hass)
    before = runtime.state
    paused = step(before, is_nt=lambda _: False).state
    with _failing(), pytest.raises(StoreNotDurableError):
        await runtime.async_commit(paused)
    assert runtime.may_enable is False
    assert runtime.persistence_fault
    assert runtime.state is before  # not adopted


async def test_later_successful_commit_does_not_clear_the_fault(storage_hass):
    runtime = await _ready_runtime(storage_hass)
    with _failing(), pytest.raises(StoreNotDurableError):
        await runtime.async_commit(runtime.state)
    await runtime.async_commit(step(runtime.state, is_nt=lambda _: False).state)
    assert runtime.may_enable is False


async def test_explicit_storage_verification_recovers_with_reconcile_required(storage_hass):
    runtime = await _ready_runtime(storage_hass)
    with _failing(), pytest.raises(StoreNotDurableError):
        await runtime.async_commit(runtime.state)
    with _failing():
        assert await runtime.async_verify_storage() is False
    assert runtime.may_enable is False

    assert await runtime.async_verify_storage() is True
    assert runtime.persistence_fault is None
    assert runtime.state.reconcile_state.value == "required"
    assert runtime.may_enable is False  # old stored state is no new authorization


async def test_verification_keeps_a_blocked_reconcile_blocked(storage_hass):
    runtime = await _ready_runtime(storage_hass)
    await runtime.async_commit(set_reconcile(runtime.state, "blocked", "counter_decreased"))
    with _failing(), pytest.raises(StoreNotDurableError):
        await runtime.async_commit(runtime.state)
    assert await runtime.async_verify_storage() is True
    assert runtime.state.reconcile_state.value == "blocked"
