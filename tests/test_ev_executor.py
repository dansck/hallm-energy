"""The wallbox writer: gate, order, abort on the first unconfirmed step, preemption, holds."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.hallm_energy.device_io import DeviceIO
from custom_components.hallm_energy.ev_executor import (
    MAX_UNPAUSES_PER_HOUR,
    RESTART_HOLD,
    EvExecutor,
)
from custom_components.hallm_energy.executor import Gates
from tests.test_executor import FakeInverter
from tests.test_inputs import CONFIG, NOW

PAUSE, INTENSITY, MAXI, DYNAMIC = ("switch.wb_pause", "number.wb_intensity", "number.wb_max",
                                   "switch.wb_dynamic")
CURRENTS = ("sensor.i_l1", "sensor.i_l2", "sensor.i_l3")
POWERS = ("sensor.p_l1", "sensor.p_l2", "sensor.p_l3")


def _config(**over):
    return replace(CONFIG, manage_ev=True, phase_current_entities=CURRENTS,
                   phase_power_entities=POWERS, ev_wallbox_pause_entity=PAUSE,
                   ev_wallbox_intensity_entity=INTENSITY, ev_wallbox_max_intensity_entity=MAXI,
                   ev_wallbox_dynamic_entity=DYNAMIC, **over)


def _wallbox(hass, *, paused="on", intensity="6", maxi="16", dynamic="on", shadow="off"):
    hass.states.async_set(PAUSE, paused)
    hass.states.async_set(INTENSITY, intensity)
    hass.states.async_set(MAXI, maxi)
    hass.states.async_set(DYNAMIC, dynamic)
    hass.states.async_set(CONFIG.shadow_entity, shadow)
    hass.states.async_set(CONFIG.feature_entity, "off")


def _executor(hass, config=None):
    io = DeviceIO(hass, call_timeout_s=0.3, readback_timeout_s=0.3, poll_interval_s=0.02)
    cfg = config or _config()
    return EvExecutor(io, cfg, Gates(hass, shadow_entity=cfg.shadow_entity,
                                     feature_entity=cfg.feature_entity))


def _step(target, value, phase="prepare"):
    return {"target": target, "value": value, "reason": "t", "phase": phase}


FORCE = [_step("dynamic", False), _step("intensity", 16.0), _step("pause", False, "enable")]


def _report(state="force_charge", intents=None):
    return {"state": state, "intents": FORCE if intents is None else intents}


def _same(report):
    return lambda: report


@pytest.fixture
def device(storage_hass):
    fake = FakeInverter(storage_hass)
    _wallbox(storage_hass)
    return fake


# --- the gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize("over", [{"manage_ev": False}, {"phase_current_entities": ()},
                                  {"ev_wallbox_dynamic_entity": None}])
async def test_without_every_part_of_the_gate_nothing_is_written(storage_hass, device, over):
    executor = _executor(storage_hass, replace(_config(), **over))
    report = _report()
    out = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "shadow" and out.live is False and device.calls == []


async def test_the_shadow_switch_on_keeps_the_gate_closed(storage_hass, device):
    _wallbox(storage_hass, shadow="on")
    report = _report()
    out = await _executor(storage_hass).async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "shadow" and device.calls == []


async def test_a_plan_without_steps_writes_nothing(storage_hass, device):
    out = await _executor(storage_hass).async_apply(
        _report(intents=[]), now=NOW, recheck=lambda: None)
    assert out.result == "nothing_to_do" and device.calls == []


# --- order and confirmation -------------------------------------------------------------------


async def test_a_forced_charge_is_written_in_order_and_unpaused_last(storage_hass, device):
    report = _report()
    out = await _executor(storage_hass).async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "done"
    assert device.calls == [(DYNAMIC, False), (INTENSITY, 16.0), (PAUSE, False)]
    assert [device.value(e) for e in (DYNAMIC, INTENSITY, PAUSE)] == ["off", "16.0", "off"]


async def test_a_value_already_in_place_is_not_written_again(storage_hass, device):
    _wallbox(storage_hass, paused="on", intensity="16", dynamic="off")
    report = _report()
    out = await _executor(storage_hass).async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "done" and device.calls == [(PAUSE, False)]


@pytest.mark.parametrize("mode", ["reject", "ignore"])
async def test_a_failed_preparation_never_leads_to_an_unpause(storage_hass, device, mode):
    getattr(device, mode).add((INTENSITY, 16.0))
    report = _report()
    out = await _executor(storage_hass).async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "aborted" and (PAUSE, False) not in device.calls
    assert device.value(PAUSE) == "on"


async def test_a_plan_that_changed_before_a_step_stops_the_sequence(storage_hass, device):
    report = _report()
    changed = {"state": "idle", "intents": [_step("pause", True, "protect")]}
    out = await _executor(storage_hass).async_apply(report, now=NOW, recheck=lambda: changed)
    assert out.result == "aborted" and out.reason == "the plan changed"
    assert device.calls == []


async def test_a_missing_plan_stops_the_sequence_too(storage_hass, device):
    report = _report()
    out = await _executor(storage_hass).async_apply(report, now=NOW, recheck=lambda: None)
    assert out.result == "aborted" and device.calls == []


# --- protection -------------------------------------------------------------------------------


async def test_a_protective_pause_needs_no_recheck_and_is_idempotent(storage_hass, device):
    _wallbox(storage_hass, paused="off")
    report = _report("curtailed", [_step("pause", True, "protect")])
    executor = _executor(storage_hass)
    first = await executor.async_apply(report, now=NOW, recheck=lambda: None)
    again = await executor.async_apply(report, now=NOW, recheck=lambda: None)
    assert first.result == again.result == "done" and device.calls == [(PAUSE, True)]


async def test_a_protective_pause_preempts_a_sequence_that_is_in_flight(storage_hass, device):
    gate = asyncio.Event()
    device.stall[(INTENSITY, 16.0)] = gate
    executor = _executor(storage_hass)
    executor.io.call_timeout_s = executor.io.readback_timeout_s = 5.0
    report = _report()
    running = asyncio.create_task(
        executor.async_apply(report, now=NOW, recheck=_same(report)))
    await asyncio.sleep(0.1)
    _wallbox_paused_state = storage_hass.states.get(PAUSE).state
    assert _wallbox_paused_state == "on"
    protect = asyncio.create_task(executor.async_apply(
        _report("curtailed", [_step("pause", True, "protect")]), now=NOW, recheck=lambda: None))
    await asyncio.sleep(0.05)
    gate.set()
    first, second = await asyncio.gather(running, protect)
    assert first.result == "preempted" and (PAUSE, False) not in device.calls
    assert second.result == "done" and storage_hass.states.get(PAUSE).state == "on"


# --- holds ------------------------------------------------------------------------------------


async def test_routine_charging_does_not_restart_right_after_a_pause(storage_hass, device):
    executor = _executor(storage_hass)
    _wallbox(storage_hass, paused="off")
    await executor.async_apply(_report("curtailed", [_step("pause", True, "protect")]),
                               now=NOW, recheck=lambda: None)
    routine = _report("mobility_floor", [_step("pause", False, "enable")])
    soon = NOW + RESTART_HOLD - timedelta(seconds=1)
    out = await executor.async_apply(routine, now=soon, recheck=_same(routine))
    assert out.result == "held" and storage_hass.states.get(PAUSE).state == "on"
    later = NOW + RESTART_HOLD
    out = await executor.async_apply(routine, now=later, recheck=_same(routine))
    assert out.result == "done" and storage_hass.states.get(PAUSE).state == "off"


async def test_a_forced_charge_restarts_at_once(storage_hass, device):
    executor = _executor(storage_hass)
    _wallbox(storage_hass, paused="off", intensity="16", dynamic="off")
    await executor.async_apply(_report("curtailed", [_step("pause", True, "protect")]),
                               now=NOW, recheck=lambda: None)
    forced = _report("force_charge", [_step("pause", False, "enable")])
    out = await executor.async_apply(forced, now=NOW + timedelta(seconds=5),
                                     recheck=_same(forced))
    assert out.result == "done"


async def test_routine_restarts_are_limited_per_hour(storage_hass, device):
    executor = _executor(storage_hass)
    routine = _report("mobility_floor", [_step("pause", False, "enable")])
    t = NOW
    for _ in range(MAX_UNPAUSES_PER_HOUR):
        _wallbox(storage_hass, paused="on", intensity="16", dynamic="off")
        out = await executor.async_apply(routine, now=t, recheck=_same(routine))
        assert out.result == "done"
        t += timedelta(minutes=1)
    _wallbox(storage_hass, paused="on", intensity="16", dynamic="off")
    out = await executor.async_apply(routine, now=t, recheck=_same(routine))
    assert out.result == "held" and out.reason == "too many restarts in the last hour"
    out = await executor.async_apply(routine, now=NOW + timedelta(hours=1, minutes=2),
                                     recheck=_same(routine))
    assert out.result == "done"


# --- through the controller ---------------------------------------------------------------------

SOC, PLUG = "sensor.car_soc", "binary_sensor.car_plug"


async def _live_controller(hass, *, config=None):
    from custom_components.hallm_energy.ev_flow import EvShadow
    from custom_components.hallm_energy.guard_flow import GuardShadow
    from tests.test_controller import _controller, _fresh_state

    controller = await _controller(hass, _fresh_state(), live=True)
    controller.config = config or replace(_config(), ev_soc_entity=SOC, ev_connected_entity=PLUG)
    controller.guard = GuardShadow(controller.config)
    controller.ev = EvShadow(hass, controller.config)
    controller.ev_exec = _executor(hass, controller.config)
    for entity, value in zip(CURRENTS + POWERS, (6, 7, 8, 1000, 1000, 1000), strict=True):
        hass.states.async_set(entity, str(value))
    hass.states.async_set(SOC, "60")
    hass.states.async_set(PLUG, "on")
    return controller


async def test_a_fast_cycle_carries_out_a_forced_charge(storage_hass, device):
    controller = await _live_controller(storage_hass)
    controller.ev_force = True
    await controller.async_ev_cycle()
    assert controller.ev_report["state"] == "force_charge"
    assert controller.ev_exec.last.result == "done"
    assert device.calls == [(DYNAMIC, False), (INTENSITY, 16.0), (PAUSE, False)]


async def test_a_guard_that_pauses_the_ev_pauses_the_wallbox_at_once(storage_hass, device):
    _wallbox(storage_hass, paused="off", intensity="16", dynamic="off")
    controller = await _live_controller(storage_hass)
    controller.ev_force = True
    # an import excess that lasts: after the hold the guard steps down to a pause
    for entity, value in zip(CURRENTS, (23, 6, 7), strict=True):
        storage_hass.states.async_set(entity, str(value))
    controller.guard._state = replace(controller.guard._state, ev_current_cap_a=6.0,
                                      import_over_since=NOW - timedelta(seconds=60))
    storage_hass.states.async_set(POWERS[0], "5000")
    await controller.async_ev_cycle()
    assert controller.guard_report["ev_paused"] is True
    assert controller.ev_report["state"] == "curtailed"
    assert (PAUSE, True) in device.calls and storage_hass.states.get(PAUSE).state == "on"


async def test_a_stale_guard_blocks_charging_but_not_a_pause(storage_hass, device):
    controller = await _live_controller(storage_hass)
    controller.ev_force = True
    stale = {"stale": True, "ev_current_cap_a": 16.0, "ev_paused": False,
             "grid_charge_allowed": True, "export_cap_w": 8000.0, "restricted": False}
    controller.guard.evaluate = lambda *_a, **_k: stale
    await controller.async_ev_cycle()
    assert controller.ev_report["intents_withheld"] == "current ceiling unavailable"
    assert device.calls == []  # nothing is let through on stale measurements


async def test_with_the_gate_closed_the_cycle_only_reports(storage_hass, device):
    controller = await _live_controller(storage_hass, config=replace(
        _config(), manage_ev=False, ev_soc_entity=SOC, ev_connected_entity=PLUG))
    controller.ev_force = True
    await controller.async_ev_cycle()
    assert controller.ev_report["state"] == "force_charge"
    assert controller.ev_exec.last.result == "shadow" and device.calls == []


async def test_a_failed_write_is_not_retried_every_second_but_a_protective_pause_is_not_held(
        storage_hass, device):
    device.reject.add((INTENSITY, 16.0))
    executor = _executor(storage_hass)
    report = _report()
    first = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert first.result == "aborted"
    calls = len(device.calls)
    soon = await executor.async_apply(report, now=NOW + timedelta(seconds=5),
                                      recheck=_same(report))
    assert soon.result == "backoff" and len(device.calls) == calls
    protect = await executor.async_apply(
        _report("curtailed", [_step("pause", True, "protect")]), now=NOW + timedelta(seconds=6),
        recheck=lambda: None)
    assert protect.result == "done"  # (the wallbox was paused already: nothing to write)
    device.reject.clear()
    later = await executor.async_apply(report, now=NOW + timedelta(seconds=31),
                                       recheck=_same(report))
    assert later.result == "done" and device.value(PAUSE) == "off"


async def test_nothing_is_enabled_while_the_house_battery_charges_from_the_grid(
        storage_hass, device):
    busy = {"on": True}
    io = DeviceIO(storage_hass, call_timeout_s=0.3, readback_timeout_s=0.3, poll_interval_s=0.02)
    cfg = _config()
    executor = EvExecutor(io, cfg, Gates(storage_hass, shadow_entity=cfg.shadow_entity,
                                         feature_entity=cfg.feature_entity),
                          grid_busy=lambda: busy["on"])
    report = _report()
    held = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert held.result == "held" and "grid charging" in held.reason and device.calls == []
    pause = await executor.async_apply(
        _report("curtailed", [_step("pause", True, "protect")]), now=NOW, recheck=lambda: None)
    assert pause.result == "done"  # protection is never held
    busy["on"] = False
    _wallbox(storage_hass)
    ok = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert ok.result == "done"


async def test_the_controller_counts_a_running_grant_or_a_charging_inverter_as_busy(
        storage_hass, device):
    controller = await _live_controller(storage_hass)
    storage_hass.states.async_set(controller.config.grid_charge_entity, "off")
    assert controller._grid_charging_busy() is False
    storage_hass.states.async_set(controller.config.grid_charge_entity, "on")
    assert controller._grid_charging_busy() is True
    storage_hass.states.async_set(controller.config.grid_charge_entity, "unavailable")
    assert controller._grid_charging_busy() is True  # unknown counts as busy: nothing is assumed
