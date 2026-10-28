"""Executor + device adapter against a real test Home Assistant with a fake
inverter (services that update entity states, or reject / ignore / stall)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import time, timedelta
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.file import WriteError

from custom_components.hallm_energy import store as store_module
from custom_components.hallm_energy.const import STORAGE_KEY
from custom_components.hallm_energy.device_io import DeviceIO
from custom_components.hallm_energy.energy_core.grant import CounterReading
from custom_components.hallm_energy.executor import Gates, GrantExecutor, Snapshot
from custom_components.hallm_energy.lifecycle import set_reconcile
from custom_components.hallm_energy.runtime import EnergyRuntime
from custom_components.hallm_energy.slot_plan import Op, SlotEntities
from custom_components.hallm_energy.store import LocalStateStore
from tests.test_lifecycle import COUNTER, T0, empty_state, prepared, step

GRID = "switch.inv_grid_charge"
CAPS = tuple(f"number.inv_slot{i}_capacity" for i in range(1, 7))
CES = tuple(f"switch.inv_slot{i}_charge_enable" for i in range(1, 7))
ENTITIES = SlotEntities(grid_charge=GRID, slot_capacity=CAPS, slot_charge_enable=CES)
SHADOW, FEATURE = "input_boolean.shadow", "input_boolean.grid_recovery"
NIGHT_SLOT = 5  # 22:15 local falls into the slot starting 17:00


class FakeInverter:
    def __init__(self, hass):
        self.hass = hass
        self.calls: list[tuple[str, object]] = []
        self.reject: set[tuple[str, object]] = set()
        self.ignore: set[tuple[str, object]] = set()
        self.drop: set[tuple[str, object]] = set()  # device falls off the bus
        self.after: dict[tuple[str, object], object] = {}  # hook run after a call
        self.stall: dict[tuple[str, object], asyncio.Event] = {}
        hass.states.async_set(GRID, "off")
        for cap, ce in zip(CAPS, CES, strict=True):
            hass.states.async_set(cap, "20")
            hass.states.async_set(ce, "off")
        hass.services.async_register("switch", "turn_on", self._switch(True))
        hass.services.async_register("switch", "turn_off", self._switch(False))
        hass.services.async_register("number", "set_value", self._set_value)

    async def _apply(self, entity_id, value, state):
        key = (entity_id, value)
        self.calls.append(key)
        if key in self.stall:
            await self.stall[key].wait()
        if key in self.reject:
            raise HomeAssistantError("device refused")
        if key in self.drop:
            self.hass.states.async_set(entity_id, "unavailable")
        elif key not in self.ignore:
            self.hass.states.async_set(entity_id, state)
        if key in self.after:
            self.after.pop(key)()

    def _switch(self, on):
        async def handler(call):
            await self._apply(call.data["entity_id"], on, "on" if on else "off")
        return handler

    async def _set_value(self, call):
        value = float(call.data["value"])
        await self._apply(call.data["entity_id"], value, str(value))

    def value(self, entity_id):
        return self.hass.states.get(entity_id).state


@pytest.fixture
async def inverter(storage_hass):
    return FakeInverter(storage_hass)


def _gates(hass, *, shadow="off", feature="on") -> Gates:
    if shadow is not None:
        hass.states.async_set(SHADOW, shadow)
    if feature is not None:
        hass.states.async_set(FEATURE, feature)
    return Gates(hass, shadow_entity=SHADOW, feature_entity=FEATURE)


async def _executor(hass, *, gates=None, state=None, **io) -> GrantExecutor:
    runtime = EnergyRuntime(hass, LocalStateStore(hass, version=1, key=STORAGE_KEY))
    await runtime.async_commit(state or set_reconcile(prepared(), "ready", None))
    device = DeviceIO(hass, call_timeout_s=io.get("call", 0.3),
                      readback_timeout_s=io.get("readback", 0.3), poll_interval_s=0.02)
    return GrantExecutor(runtime, device, ENTITIES, gates or _gates(hass))


def _snapshot(minutes=15, soc=19.0, raw=7204.0, nt=True) -> Snapshot:
    now = T0 + timedelta(minutes=minutes)
    return Snapshot(
        now=now,
        local_time=time(22, minutes % 60),
        soc_pct=soc,
        telemetry_valid=True,
        reading=CounterReading(COUNTER, raw, now),
        reading_epoch="e1",
        is_nt=lambda _: nt,
    )


def _enabled_values(inverter):
    return inverter.value(GRID), inverter.value(CES[NIGHT_SLOT]), inverter.value(CAPS[NIGHT_SLOT])


# --- gates / shadow -----------------------------------------------------------------


async def test_shadow_reports_preparation_and_writes_nothing(storage_hass, inverter):
    executor = await _executor(storage_hass, gates=_gates(storage_hass, shadow="on"))
    report = await executor.async_cycle(_snapshot())
    assert report.live is False
    assert inverter.calls == []
    assert Op(CAPS[0], 25.0) in report.intents  # prepare would raise floors
    assert report.reason == "deye_not_prepared"


async def test_shadow_reports_enable_but_persists_no_proposal(storage_hass, inverter):
    for cap in CAPS:  # inverter already prepared by hand
        storage_hass.states.async_set(cap, "25")
    executor = await _executor(storage_hass, gates=_gates(storage_hass, shadow="on"))
    report = await executor.async_cycle(_snapshot())
    assert inverter.calls == []
    assert report.intents[-1] == Op(GRID, True)
    assert executor.runtime.state.pending_step is None
    assert executor.runtime.state.active_grant.status == "prepared"


@pytest.mark.parametrize(
    "shadow,feature",
    [(None, "on"), ("unavailable", "on"), ("off", None), ("off", "off"), ("off", "unknown")],
)
async def test_gates_not_explicitly_live_mean_no_writes(storage_hass, inverter, shadow, feature):
    executor = await _executor(storage_hass, gates=_gates(storage_hass, shadow=shadow,
                                                          feature=feature))
    report = await executor.async_cycle(_snapshot())
    assert report.live is False
    assert inverter.calls == []


async def test_reconcile_not_ready_means_no_writes_even_when_live(storage_hass, inverter):
    executor = await _executor(storage_hass, state=set_reconcile(prepared(), "required", "startup"))
    await executor.async_cycle(_snapshot())
    assert inverter.calls == []


# --- live enable ----------------------------------------------------------------------


async def test_live_cycle_prepares_then_enables_with_confirmed_readback(storage_hass, inverter):
    executor = await _executor(storage_hass)
    report = await executor.async_cycle(_snapshot())
    assert report.step_outcome == "confirmed"
    assert all(inverter.value(c) == "25.0" for c in CAPS)  # every floor raised first
    assert _enabled_values(inverter) == ("on", "on", "25.0")
    assert inverter.calls[-1] == (GRID, True)  # grid_charge last
    state = executor.runtime.state
    assert state.active_grant.status == "active"
    assert state.pending_step is None


async def test_enable_uses_slot_starts_read_from_the_inverter(storage_hass, inverter):
    executor = await _executor(storage_hass)
    starts = (time(0), time(1), time(2), time(3), time(4), time(23))  # 22:15 → slot index 4
    report = await executor.async_cycle(replace(_snapshot(), slot_starts=starts))
    assert report.step_outcome == "confirmed"
    assert inverter.value(CES[4]) == "on"
    assert inverter.value(CES[NIGHT_SLOT]) == "off"


async def test_rejected_enable_is_failed_and_cleaned_up(storage_hass, inverter):
    inverter.reject.add((GRID, True))
    executor = await _executor(storage_hass)
    report = await executor.async_cycle(_snapshot())
    assert report.step_outcome == "failed"
    assert inverter.value(GRID) == "off"
    assert inverter.value(CES[NIGHT_SLOT]) == "off"  # half-enabled slot switched back off
    assert executor.runtime.state.active_grant.status == "paused"
    assert executor.runtime.state.pending_step is None


async def test_ignored_write_without_readback_is_not_confirmed(storage_hass, inverter):
    inverter.ignore.add((CES[NIGHT_SLOT], True))
    executor = await _executor(storage_hass)
    report = await executor.async_cycle(_snapshot())
    assert report.step_outcome != "confirmed"
    assert executor.runtime.state.active_grant.status != "active"
    assert (GRID, True) not in inverter.calls  # never got as far as the master switch


async def test_unconfirmable_stop_leaves_outcome_unknown_and_reconcile_required(
    storage_hass, inverter
):
    # grid_charge drops off right when switched on, and the slot cannot be
    # read back after the stop either: charging may or may not be possible.
    inverter.drop.add((GRID, True))
    inverter.after[(GRID, True)] = lambda: inverter.drop.update(
        {(GRID, False), (CES[NIGHT_SLOT], False)}
    )
    executor = await _executor(storage_hass)
    report = await executor.async_cycle(_snapshot())
    assert report.step_outcome == "unknown"
    state = executor.runtime.state
    assert state.pending_step is not None
    assert state.reconcile_state.value == "required"
    assert executor.runtime.may_enable is False


# --- safety stop preemption ---------------------------------------------------------


async def test_safety_stop_preempts_an_enabling_sequence(storage_hass, inverter):
    release = asyncio.Event()
    inverter.stall[(CES[NIGHT_SLOT], True)] = release
    executor = await _executor(storage_hass, call=5, readback=0.3)
    cycle = asyncio.create_task(executor.async_cycle(_snapshot()))
    while (CES[NIGHT_SLOT], True) not in inverter.calls:
        await asyncio.sleep(0.01)

    stop = asyncio.create_task(executor.async_safety_stop())
    await asyncio.sleep(0.05)
    release.set()
    await stop
    report = await cycle

    assert (GRID, True) not in inverter.calls  # aborted before the master switch
    assert inverter.value(GRID) == "off"
    assert inverter.value(CES[NIGHT_SLOT]) == "off"
    assert report.step_outcome != "confirmed"
    assert executor.runtime.state.active_grant.status != "active"


async def test_safety_stop_writes_even_when_store_is_failing(storage_hass, inverter):
    executor = await _executor(storage_hass)
    await executor.async_cycle(_snapshot())
    with patch.object(store_module, "write_utf8_file_atomic", side_effect=WriteError("disk")):
        results = await executor.async_safety_stop()
    assert all(r.outcome == "confirmed" for r in results)
    assert inverter.value(GRID) == "off"


# --- stops from the lifecycle ---------------------------------------------------------


async def test_target_soc_stops_then_closes_after_confirmed_off(storage_hass, inverter):
    executor = await _executor(storage_hass)
    await executor.async_cycle(_snapshot())
    report = await executor.async_cycle(_snapshot(minutes=30, soc=25.0))
    assert report.reason == "target_reached"
    assert inverter.value(GRID) == "off"
    state = executor.runtime.state
    assert state.active_grant is None
    assert state.episode_ledger[-1].state == "closed"


async def test_off_peak_end_pauses_and_switches_off(storage_hass, inverter):
    executor = await _executor(storage_hass)
    await executor.async_cycle(_snapshot())
    await executor.async_cycle(_snapshot(minutes=30, nt=False))
    assert inverter.value(GRID) == "off"
    assert executor.runtime.state.active_grant.status == "paused"


async def test_persist_failure_blocks_enabling_and_stops_if_charging(storage_hass, inverter):
    executor = await _executor(storage_hass)
    await executor.async_cycle(_snapshot())
    assert inverter.value(GRID) == "on"
    with patch.object(store_module, "write_utf8_file_atomic", side_effect=WriteError("disk")):
        report = await executor.async_cycle(_snapshot(minutes=30, raw=7205.0))
    assert report.error and "not persisted" in report.error
    assert inverter.value(GRID) == "off"
    assert executor.runtime.may_enable is False
    calls_before = len(inverter.calls)
    await executor.async_cycle(_snapshot(minutes=45, raw=7205.0))
    assert (GRID, True) not in inverter.calls[calls_before:]


# --- device adapter ---------------------------------------------------------------------


async def test_device_io_outcomes(storage_hass, inverter):
    io = DeviceIO(storage_hass, call_timeout_s=0.2, readback_timeout_s=0.2, poll_interval_s=0.02)
    assert (await io.write(Op(GRID, True))).outcome == "confirmed"
    inverter.reject.add((GRID, False))
    assert (await io.write(Op(GRID, False))).outcome == "failed"
    inverter.stall[(CAPS[0], 30.0)] = asyncio.Event()  # never released → call timeout
    assert (await io.write(Op(CAPS[0], 30.0))).outcome == "unknown"
    with pytest.raises(ValueError):
        await io.write(Op("light.kitchen", True))


async def test_readback_maps_unavailable_to_unknown(storage_hass, inverter):
    storage_hass.states.async_set(GRID, "unavailable")
    readback = DeviceIO(storage_hass).read_slots(ENTITIES)
    assert readback.grid_charge is None
    assert readback.charging_enabled is False  # every slot permission is off
    storage_hass.states.async_set(CES[0], "unavailable")
    assert DeviceIO(storage_hass).read_slots(ENTITIES).charging_enabled is None


def test_slot_entities_validation():
    with pytest.raises(ValueError):
        replace(ENTITIES, slot_capacity=CAPS[:5])



# --- floors after a recovery grant -------------------------------------------------------


def _after_grant_state(kind="restoring"):
    from tests.test_recovery_flow import _closed

    _, closed = _closed()
    recovery = replace(closed.recovery, state=kind)
    return set_reconcile(replace(closed, recovery=recovery), "ready", None)


def _raise_all(hass, value="25.0"):
    for cap in CAPS:
        hass.states.async_set(cap, value)


async def test_restoring_writes_restore_floors_when_live(storage_hass, inverter):
    _raise_all(storage_hass)
    executor = await _executor(storage_hass, state=_after_grant_state())
    report = await executor.async_cycle(_snapshot(soc=25.0))
    assert [float(inverter.value(c)) for c in CAPS] == [20.0] * 6  # never below the reserve
    assert all(w.outcome == "confirmed" for w in report.writes)
    assert {w.op.entity_id for w in report.writes} == set(CAPS)


async def test_hold_raises_back_a_partially_restored_floor(storage_hass, inverter):
    _raise_all(storage_hass)
    storage_hass.states.async_set(CAPS[0], "20.0")  # restored before the forecast turned
    executor = await _executor(storage_hass, state=_after_grant_state("cloudy_hold"))
    await executor.async_cycle(_snapshot(soc=25.0))
    assert [float(inverter.value(c)) for c in CAPS] == [25.0] * 6


async def test_floor_changed_from_outside_blocks_every_write(storage_hass, inverter):
    _raise_all(storage_hass)
    storage_hass.states.async_set(CAPS[2], "15.0")
    executor = await _executor(storage_hass, state=_after_grant_state())
    report = await executor.async_cycle(_snapshot(soc=25.0))
    assert inverter.calls == [] and "floor_conflict" in report.error


async def test_restoring_in_shadow_only_reports(storage_hass, inverter):
    _raise_all(storage_hass)
    executor = await _executor(storage_hass, state=_after_grant_state(),
                               gates=_gates(storage_hass, shadow="on"))
    report = await executor.async_cycle(_snapshot(soc=25.0))
    assert inverter.calls == []
    assert [op.value for op in report.intents if op.entity_id in CAPS] == [20.0] * 6


async def test_restoring_waits_while_charging_is_on(storage_hass, inverter):
    _raise_all(storage_hass)
    storage_hass.states.async_set(GRID, "on")
    storage_hass.states.async_set(CES[NIGHT_SLOT], "on")
    executor = await _executor(storage_hass, state=_after_grant_state(),
                               gates=_gates(storage_hass, shadow="on"))
    report = await executor.async_cycle(_snapshot(soc=25.0))
    assert not [op for op in report.intents if op.entity_id in CAPS]


# --- managed floors (the table plan) -----------------------------------------------------


def _plan(floors, *, below=(), reserve=20.0):
    from custom_components.hallm_energy.floor_table import FloorPlan

    return FloorPlan(tuple(floors), ("vt",) * len(floors), (), reserve_pct=reserve,
                     below_reserve_slots=tuple(below))


def _set_caps(hass, values):
    for cap, value in zip(CAPS, values, strict=True):
        hass.states.async_set(cap, str(value))


async def _idle_executor(hass, **kw):
    return await _executor(hass, state=set_reconcile(empty_state(), "ready", None), **kw)


async def test_table_plan_raises_always_and_lowers_only_when_allowed(storage_hass, inverter):
    _set_caps(storage_hass, [10, 20, 30, 30, 25, 25])
    executor = await _idle_executor(storage_hass)
    plan = _plan([25, 25, 25, 25, 25, 20])
    report = await executor.async_cycle(_snapshot(soc=50.0), floor_plan=plan, lowering_ok=False)
    assert [float(inverter.value(c)) for c in CAPS] == [25, 25, 30, 30, 25, 25]  # raised only
    assert {w.op.entity_id for w in report.writes} == {CAPS[0], CAPS[1]}
    report = await executor.async_cycle(_snapshot(soc=50.0), floor_plan=plan, lowering_ok=True)
    assert [float(inverter.value(c)) for c in CAPS] == [25, 25, 25, 25, 25, 20]


async def test_table_plan_writes_raises_before_lowers(storage_hass, inverter):
    _set_caps(storage_hass, [10, 30, 25, 25, 25, 25])
    executor = await _idle_executor(storage_hass)
    await executor.async_cycle(_snapshot(soc=50.0), floor_plan=_plan([25, 25, 25, 25, 25, 20]),
                               lowering_ok=True)
    values = [c[1] for c in inverter.calls]
    assert [c[0] for c in inverter.calls] == [CAPS[0], CAPS[1], CAPS[5]] and values == [25, 25, 20]


async def test_a_floor_below_the_reserve_needs_an_exception_to_be_written(storage_hass, inverter):
    _set_caps(storage_hass, [25] * 6)
    executor = await _idle_executor(storage_hass)
    report = await executor.async_cycle(_snapshot(soc=50.0), lowering_ok=True,
                                        floor_plan=_plan([10, 25, 25, 25, 25, 20]))
    assert inverter.calls == [] and "below_reserve_without_exception" in report.error
    report = await executor.async_cycle(_snapshot(soc=50.0), lowering_ok=True,
                                        floor_plan=_plan([10, 25, 25, 25, 25, 20], below=(0,)))
    assert inverter.value(CAPS[0]) == "10.0" and report.error is None


@pytest.mark.parametrize("blocker", ["grant", "charging", "pending", "not_ready", "shadow"])
async def test_table_plan_is_never_written_when_something_else_owns_the_floors(
        storage_hass, inverter, blocker):
    """The plan floors are 35: the grant's own preparation writes 25 (its target), so a
    35 can only come from the table."""
    from tests.test_lifecycle import prepared

    _set_caps(storage_hass, [10] * 6)
    state = set_reconcile(empty_state(), "ready", None)
    gates = None
    if blocker == "grant":
        state = set_reconcile(prepared(), "ready", None)
    elif blocker == "pending":
        state = set_reconcile(step(prepared()).state, "ready", None)
    elif blocker == "not_ready":
        state = set_reconcile(empty_state(), "required", "startup")
    elif blocker == "shadow":
        gates = _gates(storage_hass, shadow="on")
    executor = await _executor(storage_hass, state=state, gates=gates)
    if blocker == "charging":
        # charging is on and the device ignores every switch-off: it cannot be confirmed off
        storage_hass.states.async_set(GRID, "on")
        storage_hass.states.async_set(CES[NIGHT_SLOT], "on")
        inverter.ignore |= {(GRID, False), *((ce, False) for ce in CES)}
    # SOC below the grant target keeps the grant open (above it the grant would close)
    report = await executor.async_cycle(_snapshot(soc=19.0), floor_plan=_plan([35] * 6),
                                        lowering_ok=True)
    assert not [c for c in inverter.calls if c[0] in CAPS and c[1] == 35.0]
    if blocker != "shadow":  # shadow reports the table's intent, by design
        assert not [op for op in report.intents if op.entity_id in CAPS and op.value == 35.0]


async def test_after_a_confirmed_stop_the_table_plan_may_be_written(storage_hass, inverter):
    _set_caps(storage_hass, [10] * 6)
    storage_hass.states.async_set(GRID, "on")
    storage_hass.states.async_set(CES[NIGHT_SLOT], "on")
    executor = await _idle_executor(storage_hass)
    await executor.async_cycle(_snapshot(soc=50.0), floor_plan=_plan([35] * 6))
    assert inverter.value(GRID) == "off"  # stopped first (no grant allows charging)
    assert [float(inverter.value(c)) for c in CAPS] == [35.0] * 6


async def test_shadow_only_reports_the_table_writes(storage_hass, inverter):
    _set_caps(storage_hass, [10] * 6)
    executor = await _idle_executor(storage_hass, gates=_gates(storage_hass, shadow="on"))
    report = await executor.async_cycle(_snapshot(soc=50.0), floor_plan=_plan([25] * 6))
    assert inverter.calls == []
    assert [op.value for op in report.intents if op.entity_id in CAPS] == [25.0] * 6


async def test_unreadable_floor_gets_the_planned_value_and_a_failed_write_stops_the_rest(
        storage_hass, inverter):
    storage_hass.states.async_set(CAPS[1], "unavailable")
    _set_caps_partial = [10, None, 10, 10, 10, 10]
    for cap, value in zip(CAPS, _set_caps_partial, strict=True):
        if value is not None:
            storage_hass.states.async_set(cap, str(value))
    executor = await _idle_executor(storage_hass)
    inverter.ignore.add((CAPS[2], 25.0))  # the third write is not applied by the device
    await executor.async_cycle(_snapshot(soc=50.0), floor_plan=_plan([25] * 6), lowering_ok=True)
    assert [c[0] for c in inverter.calls] == [CAPS[0], CAPS[1], CAPS[2]]  # stopped there
