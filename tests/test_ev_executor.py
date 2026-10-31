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


# --- the limit on the car ------------------------------------------------------------------------

LIMIT = "number.car_limit"


def _limit_writer(hass, **over):
    from custom_components.hallm_energy.ev_limit import CarLimitWriter

    cfg = replace(_config(), **{"ev_limit_entity": LIMIT, **over})
    io = DeviceIO(hass, call_timeout_s=0.3, readback_timeout_s=0.3, poll_interval_s=0.02)
    return CarLimitWriter(hass, cfg, Gates(hass, shadow_entity=cfg.shadow_entity,
                                           feature_entity=cfg.feature_entity), io=io)


async def test_the_car_limit_is_written_in_the_background_and_confirmed(storage_hass, device):
    storage_hass.states.async_set(LIMIT, "80")
    writer = _limit_writer(storage_hass)
    writer.request(100.0, NOW)
    await writer._task
    assert writer.last_result == "done" and device.value(LIMIT) == "100.0"
    writer.request(100.0, NOW)
    assert writer.last_result == "in place"


async def test_the_car_limit_is_only_reported_while_the_gate_is_closed(storage_hass, device):
    storage_hass.states.async_set(LIMIT, "80")
    writer = _limit_writer(storage_hass, manage_ev=False)
    writer.request(100.0, NOW)
    assert writer.last_result == "shadow" and device.calls == []
    writer = _limit_writer(storage_hass)
    storage_hass.states.async_set(CONFIG.shadow_entity, "on")
    writer.request(100.0, NOW)
    assert writer.last_result == "shadow" and device.calls == []


async def test_a_second_request_does_not_start_a_second_write(storage_hass, device):
    storage_hass.states.async_set(LIMIT, "80")
    gate = asyncio.Event()
    device.stall[(LIMIT, 100.0)] = gate
    writer = _limit_writer(storage_hass)
    writer.io.call_timeout_s = writer.io.readback_timeout_s = 5.0
    writer.request(100.0, NOW)
    await asyncio.sleep(0.05)
    writer.request(100.0, NOW)
    assert writer.last_result == "busy" and len(device.calls) == 1
    gate.set()
    await writer._task


async def test_a_limit_write_that_fails_waits_before_the_next_attempt(storage_hass, device):
    from custom_components.hallm_energy.ev_limit import RETRY_AFTER

    storage_hass.states.async_set(LIMIT, "80")
    device.reject.add((LIMIT, 100.0))
    writer = _limit_writer(storage_hass)
    writer.request(100.0, NOW)
    await writer._task
    assert writer.last_result == "failed"
    writer.request(100.0, NOW + timedelta(minutes=1))
    assert writer.last_result == "backoff" and len(device.calls) == 1
    device.reject.clear()
    writer.request(100.0, NOW + RETRY_AFTER)
    await writer._task
    assert writer.last_result == "done"


async def test_no_limit_entity_or_no_desired_value_writes_nothing(storage_hass, device):
    writer = _limit_writer(storage_hass, ev_limit_entity=None)
    writer.request(100.0, NOW)
    writer = _limit_writer(storage_hass)
    writer.request(None, NOW)
    assert device.calls == []


async def test_the_car_limit_is_never_lowered(storage_hass, device):
    storage_hass.states.async_set(LIMIT, "100")
    writer = _limit_writer(storage_hass)
    writer.request(90.0, NOW)
    assert writer.last_result == "in place" and device.calls == []


# --- the guard and the grant: the last bit of the breaker protection ------------------------------


def _guard_report(**over):
    base = {"stale": False, "ev_current_cap_a": 16.0, "ev_paused": False,
            "grid_charge_allowed": True, "export_cap_w": 8000.0, "restricted": False}
    return {**base, **over}


async def test_the_grant_is_held_only_while_the_guard_forbids_grid_charging_or_is_blind(
        storage_hass, device):
    controller = await _live_controller(storage_hass)
    assert controller._grant_hold_reason() is None  # the guard has not reported yet
    controller.guard_report = _guard_report()
    assert controller._grant_hold_reason() is None
    controller.guard_report = _guard_report(grid_charge_allowed=False)
    assert controller._grant_hold_reason() == "guard_phase_overload"
    controller.guard_report = _guard_report(stale=True)
    assert controller._grant_hold_reason() == "guard_stale"
    controller.config = replace(controller.config, phase_current_entities=())
    assert controller._grant_hold_reason() is None  # no guard configured: nothing holds


async def test_the_guard_counts_the_car_as_charging_only_when_plugged_in_and_not_paused(
        storage_hass, device):
    controller = await _live_controller(storage_hass)
    storage_hass.states.async_set(PAUSE, "off")
    assert controller._ev_charging() is True
    storage_hass.states.async_set(PAUSE, "on")
    assert controller._ev_charging() is False
    storage_hass.states.async_set(PAUSE, "off")
    storage_hass.states.async_set(PLUG, "off")
    assert controller._ev_charging() is False


async def test_a_guard_that_forbids_grid_charging_triggers_a_cycle_at_once(storage_hass, device):
    controller = await _live_controller(storage_hass)
    storage_hass.states.async_set(controller.config.grid_charge_entity, "on")
    ticks: list[int] = []

    async def fake_tick(*_a, **_k):
        ticks.append(1)

    controller.async_tick = fake_tick
    controller.guard.evaluate = lambda *_a, **_k: _guard_report(grid_charge_allowed=False,
                                                                 restricted=True)
    controller.guard_step(NOW)
    await storage_hass.async_block_till_done()
    assert ticks == [1]
    controller.guard_step(NOW)  # nothing changed: no second cycle
    await storage_hass.async_block_till_done()
    assert ticks == [1]


async def test_while_the_battery_charges_from_the_grid_the_car_is_capped_at_twelve_amps(
        storage_hass, device):
    _wallbox(storage_hass, paused="off", intensity="16", dynamic="off")
    controller = await _live_controller(storage_hass)
    controller.ev_force = True
    storage_hass.states.async_set(controller.config.grid_charge_entity, "on")
    await controller.async_ev_cycle()
    steps = {(i["target"], i["value"]) for i in controller.ev_report["intents"]}
    assert ("intensity", 12.0) in steps and ("max_intensity", 12.0) in steps


# --- the inverter mode follows the car -----------------------------------------------------------

DEYE = "select.deye_mode"
TO_CT, TO_LOAD = "Zero export to CT", "Zero export to load"


def _deye_executor(hass, grid_busy=None):
    cfg = _config(ev_deye_mode_entity=DEYE)
    io = DeviceIO(hass, call_timeout_s=0.3, readback_timeout_s=0.3, poll_interval_s=0.02)
    return EvExecutor(io, cfg, Gates(hass, shadow_entity=cfg.shadow_entity,
                                     feature_entity=cfg.feature_entity), grid_busy=grid_busy)


async def test_the_inverter_is_switched_to_load_before_the_wallbox_is_unpaused(
        storage_hass, device):
    storage_hass.states.async_set(DEYE, TO_CT)
    plan = [_step("deye_mode", TO_LOAD), _step("pause", False, "enable")]
    report = _report("routine", plan)
    out = await _deye_executor(storage_hass).async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "done"
    assert device.calls == [(DEYE, TO_LOAD), (PAUSE, False)]
    assert device.value(DEYE) == TO_LOAD


@pytest.mark.parametrize("mode", ["reject", "ignore"])
async def test_a_failed_mode_switch_never_leads_to_an_unpause(storage_hass, device, mode):
    storage_hass.states.async_set(DEYE, TO_CT)
    getattr(device, mode).add((DEYE, TO_LOAD))
    plan = [_step("deye_mode", TO_LOAD), _step("pause", False, "enable")]
    report = _report("routine", plan)
    out = await _deye_executor(storage_hass).async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "aborted" and (PAUSE, False) not in device.calls
    assert device.value(PAUSE) == "on"


async def test_the_inverter_goes_back_to_ct_only_after_the_pause_is_confirmed(storage_hass, device):
    _wallbox(storage_hass, paused="off")
    storage_hass.states.async_set(DEYE, TO_LOAD)
    plan = [_step("pause", True, "protect"), _step("deye_mode", TO_CT, "restore")]
    report = _report("idle", plan)
    out = await _deye_executor(storage_hass).async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "done" and device.calls == [(PAUSE, True), (DEYE, TO_CT)]
    # a pause that is not confirmed leaves the inverter where it is
    device.calls.clear()
    _wallbox(storage_hass, paused="off")
    storage_hass.states.async_set(DEYE, TO_LOAD)
    device.ignore.add((PAUSE, True))
    out = await _deye_executor(storage_hass).async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "aborted" and (DEYE, TO_CT) not in device.calls


async def test_handing_the_battery_back_is_not_held_by_a_running_grant(storage_hass, device):
    storage_hass.states.async_set(DEYE, TO_LOAD)
    plan = [_step("deye_mode", TO_CT, "restore")]
    report = _report("idle", plan)
    out = await _deye_executor(storage_hass, grid_busy=lambda: True).async_apply(
        report, now=NOW, recheck=_same(report))
    assert out.result == "done" and device.value(DEYE) == TO_CT


async def test_through_the_controller_a_forced_charge_asks_for_the_load_mode_first(
        storage_hass, device):
    _wallbox(storage_hass, paused="on", intensity="16", dynamic="off")
    storage_hass.states.async_set(DEYE, TO_CT)
    cfg = replace(_config(ev_deye_mode_entity=DEYE), ev_soc_entity=SOC, ev_connected_entity=PLUG)
    controller = await _live_controller(storage_hass, config=cfg)
    controller.ev_exec = _deye_executor(storage_hass)
    controller.ev_force = True
    await controller.async_ev_cycle()
    assert controller.ev_report["state"] == "force_charge"
    assert controller.ev_report["wallbox"]["deye_mode"] == TO_CT
    assert device.calls[:1] == [(DEYE, TO_LOAD)] and device.calls[-1] == (PAUSE, False)


# --- review 43 ------------------------------------------------------------------------------------


async def test_the_inverter_is_handed_back_only_when_the_wallbox_reads_paused_right_then(
        storage_hass, device):
    _wallbox(storage_hass, paused="off")  # still running
    storage_hass.states.async_set(DEYE, TO_LOAD)
    report = _report("away", [_step("deye_mode", TO_CT, "restore")])
    out = await _deye_executor(storage_hass).async_apply(
        report, now=NOW, recheck=lambda: None)
    assert out.result == "aborted" and "not confirmed paused" in out.reason
    assert device.calls == [] and device.value(DEYE) == TO_LOAD
    _wallbox(storage_hass, paused="on")
    out = await _deye_executor(storage_hass).async_apply(report, now=NOW, recheck=lambda: None)
    assert out.result == "done" and device.value(DEYE) == TO_CT


async def test_an_unknown_position_of_a_running_charge_pauses_it_before_anything_goes_back(
        storage_hass, device):
    _wallbox(storage_hass, paused="off", intensity="16", dynamic="off")
    storage_hass.states.async_set(DEYE, TO_LOAD)
    cfg = replace(_config(ev_deye_mode_entity=DEYE), ev_soc_entity=SOC, ev_connected_entity=PLUG,
                  ev_tracker_entity="device_tracker.car")
    controller = await _live_controller(storage_hass, config=cfg)
    controller.ev_exec = _deye_executor(storage_hass)
    storage_hass.states.async_set("device_tracker.car", "unavailable")  # the tracker drops out
    await controller.async_ev_cycle()
    assert controller.ev_report["state"] == "away"
    assert device.calls == [(PAUSE, True), (DEYE, TO_CT)]


async def test_a_pause_that_is_refused_keeps_the_inverter_on_load_for_an_unknown_position(
        storage_hass, device):
    _wallbox(storage_hass, paused="off", intensity="16", dynamic="off")
    storage_hass.states.async_set(DEYE, TO_LOAD)
    device.reject.add((PAUSE, True))
    cfg = replace(_config(ev_deye_mode_entity=DEYE), ev_soc_entity=SOC, ev_connected_entity=PLUG,
                  ev_tracker_entity="device_tracker.car")
    controller = await _live_controller(storage_hass, config=cfg)
    controller.ev_exec = _deye_executor(storage_hass)
    storage_hass.states.async_set("device_tracker.car", "unavailable")
    await controller.async_ev_cycle()
    assert (DEYE, TO_CT) not in device.calls and device.value(DEYE) == TO_LOAD


async def test_grid_charging_that_begins_during_the_preparation_stops_the_start(
        storage_hass, device):
    busy = {"on": False}
    device.after[(DYNAMIC, False)] = lambda: busy.__setitem__("on", True)
    _wallbox(storage_hass, paused="on", intensity="12", maxi="12", dynamic="on")
    executor = _executor_with(storage_hass, grid_busy=lambda: busy["on"])
    plan = [_step("dynamic", False), _step("pause", False, "enable")]
    report = _report("routine", plan)
    out = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "held" and "grid charging" in out.reason
    assert (PAUSE, False) not in device.calls and device.value(PAUSE) == "on"


async def test_the_gate_closing_during_the_sequence_stops_it(storage_hass, device):
    device.after[(DYNAMIC, False)] = lambda: storage_hass.states.async_set(
        CONFIG.shadow_entity, "on")
    _wallbox(storage_hass, paused="on", intensity="12", maxi="12", dynamic="on")
    executor = _executor_with(storage_hass)
    plan = [_step("dynamic", False), _step("pause", False, "enable")]
    report = _report("routine", plan)
    out = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "aborted" and out.reason == "the gate closed"
    assert (PAUSE, False) not in device.calls


def _executor_with(hass, grid_busy=None, start_gate=None):
    cfg = _config()
    io = DeviceIO(hass, call_timeout_s=0.3, readback_timeout_s=0.3, poll_interval_s=0.02)
    return EvExecutor(io, cfg, Gates(hass, shadow_entity=cfg.shadow_entity,
                                     feature_entity=cfg.feature_entity),
                      grid_busy=grid_busy, start_gate=start_gate)


async def test_the_car_does_not_start_while_the_house_battery_is_starting(storage_hass, device):
    from custom_components.hallm_energy.start_gate import StartGate

    gate = StartGate()
    assert gate.try_acquire()  # the grant writer is in the middle of starting
    executor = _executor_with(storage_hass, start_gate=gate)
    report = _report()
    out = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "held" and "starting" in out.reason and device.calls == []
    pause = await executor.async_apply(_report("curtailed", [_step("pause", True, "protect")]),
                                       now=NOW, recheck=lambda: None)
    assert pause.result == "done"  # a protective step never asks for the gate
    gate.release()
    _wallbox(storage_hass)
    ok = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert ok.result == "done" and gate.held is False  # taken and given back


async def test_the_gate_is_given_back_after_every_outcome(storage_hass, device):
    from custom_components.hallm_energy.start_gate import StartGate

    gate = StartGate()
    executor = _executor_with(storage_hass, start_gate=gate)
    device.reject.add((INTENSITY, 16.0))
    report = _report()
    out = await executor.async_apply(report, now=NOW, recheck=_same(report))
    assert out.result == "aborted" and gate.held is False


async def test_both_writers_share_one_gate(storage_hass, device):
    from tests.test_controller import _controller, _fresh_state

    controller = await _controller(storage_hass, _fresh_state(), live=True)  # as in production
    assert controller.executor.start_gate is controller.ev_exec.start_gate
    assert controller.executor.start_gate is controller.start_gate


# --- the audit of the car's writes ------------------------------------------------------------


def _audited_executor(hass, sink):
    cfg = _config(ev_deye_mode_entity=DEYE)
    io = DeviceIO(hass, call_timeout_s=0.3, readback_timeout_s=0.3, poll_interval_s=0.02)
    return EvExecutor(io, cfg, Gates(hass, shadow_entity=cfg.shadow_entity,
                                     feature_entity=cfg.feature_entity), audit=sink.extend)


async def test_every_write_is_recorded_with_what_it_was_and_how_it_ended(storage_hass, device):
    storage_hass.states.async_set(DEYE, TO_CT)
    sink: list[dict] = []
    plan = [_step("deye_mode", TO_LOAD), _step("pause", False, "enable")]
    report = _report("routine", plan)
    out = await _audited_executor(storage_hass, sink).async_apply(
        report, now=NOW, recheck=_same(report))
    assert out.result == "done"
    assert [(e["kind"], e["target"], e["value"], e["outcome"], e["ev_state"]) for e in sink] == [
        ("ev_write", "deye_mode", TO_LOAD, "confirmed", "routine"),
        ("ev_write", "pause", False, "confirmed", "routine")]
    assert sink[0]["entity_id"] == DEYE


async def test_a_failed_write_and_the_stop_of_its_sequence_are_both_recorded(
        storage_hass, device):
    storage_hass.states.async_set(DEYE, TO_CT)
    device.reject.add((DEYE, TO_LOAD))
    sink: list[dict] = []
    plan = [_step("deye_mode", TO_LOAD), _step("pause", False, "enable")]
    report = _report("routine", plan)
    await _audited_executor(storage_hass, sink).async_apply(report, now=NOW, recheck=_same(report))
    kinds = [(e["kind"], e.get("outcome"), e.get("result")) for e in sink]
    assert kinds == [("ev_write", "failed", None), ("ev_sequence", None, "aborted")]
    assert sink[-1]["reason"] == "deye_mode failed"


async def test_holds_and_idle_outcomes_are_not_audit_events(storage_hass, device):
    sink: list[dict] = []
    executor = _audited_executor(storage_hass, sink)
    await executor.async_apply(_report(intents=[]), now=NOW, recheck=lambda: None)
    gate_closed = _executor_with(storage_hass)
    gate_closed.config = replace(gate_closed.config, manage_ev=False)
    report = _report()
    await gate_closed.async_apply(report, now=NOW, recheck=_same(report))
    held = _audited_executor(storage_hass, sink)
    held._grid_busy = lambda: True
    await held.async_apply(report, now=NOW, recheck=_same(report))
    assert sink == []


async def test_the_car_limit_write_is_recorded(storage_hass, device):
    storage_hass.states.async_set(LIMIT, "80")
    sink: list[dict] = []
    writer = _limit_writer(storage_hass)
    writer._audit = sink.extend
    writer.request(100.0, NOW)
    await writer._task
    assert [(e["kind"], e["value"], e["outcome"]) for e in sink] == [
        ("ev_limit_write", 100.0, "confirmed")]


async def test_recorded_events_reach_the_durable_audit_stamped(storage_hass, device):
    controller = await _live_controller(storage_hass)
    before = len(controller.runtime.state.pending_audit)
    controller.queue_ev_audit([{"kind": "ev_write", "target": "pause", "value": True,
                                "outcome": "confirmed"}])
    await storage_hass.async_block_till_done(wait_background_tasks=True)
    events = controller.runtime.state.pending_audit
    assert len(events) == before + 1
    event = events[-1]
    assert event.event_id.startswith("ev_write:") and event.payload["target"] == "pause"
    assert event.payload["installation_id"] and event.payload["policy_version"]  # stamped


async def test_when_the_outbox_is_nearly_full_the_car_leaves_the_room_to_the_grants(
        storage_hass, device):
    from custom_components.hallm_energy import controller as controller_module

    controller = await _live_controller(storage_hass)
    now = len(controller.runtime.state.pending_audit)
    controller_module.EV_AUDIT_RESERVE = controller_module.AUDIT_CAPACITY - now  # no room at all
    try:
        controller.queue_ev_audit([{"kind": "ev_write", "outcome": "confirmed"}])
        await storage_hass.async_block_till_done(wait_background_tasks=True)
    finally:
        controller_module.EV_AUDIT_RESERVE = 100
    assert len(controller.runtime.state.pending_audit) == now
    assert controller.ev_audit_dropped == 1


async def test_a_decision_is_recorded_when_it_changes_and_a_flapping_input_is_rate_limited(
        storage_hass, device):
    controller = await _live_controller(storage_hass)
    controller.queue_ev_audit = lambda payloads: sink.extend(payloads)
    sink: list[dict] = []
    base = {"state": "routine", "reason": None, "grid_charge": {"allowed": True},
            "effective_target_pct": 80.0, "force_requested": False}
    controller._audit_ev_decision(base, NOW)
    controller._audit_ev_decision(dict(base), NOW + timedelta(seconds=1))  # unchanged: nothing
    controller._audit_ev_decision({**base, "state": "idle"}, NOW + timedelta(seconds=2))
    assert [e["ev_state"] for e in sink] == ["routine", "idle"]
    for i in range(40):  # a flapping gate
        flap = {**base, "grid_charge": {"allowed": bool(i % 2)}}
        controller._audit_ev_decision(flap, NOW + timedelta(seconds=10 + i))
    assert len(sink) == 20 and controller.ev_audit_dropped > 0
