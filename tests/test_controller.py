"""Control loop against a real test Home Assistant and the fake inverter:
provisioning, startup reconcile, leaving live mode, external switch-off."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.hallm_energy.const import STORAGE_KEY
from custom_components.hallm_energy.controller import EnergyController, ProvisionError
from custom_components.hallm_energy.device_io import DeviceIO
from custom_components.hallm_energy.lifecycle import new_installation_state, set_reconcile
from custom_components.hallm_energy.runtime import EnergyRuntime
from custom_components.hallm_energy.store import LocalStateStore
from tests.test_executor import CAPS, CES, FEATURE, GRID, NIGHT_SLOT, SHADOW, FakeInverter
from tests.test_inputs import CONFIG, NOW, set_telemetry
from tests.test_lifecycle import COUNTER, activated, empty_state


@pytest.fixture
def inverter(storage_hass, freezer):
    freezer.move_to(NOW)
    inverter = FakeInverter(storage_hass)
    set_telemetry(storage_hass)
    return inverter


def _gates(hass, *, live: bool) -> None:
    hass.states.async_set(SHADOW, "off" if live else "on")
    hass.states.async_set(FEATURE, "on")


async def _controller(hass, state, *, live=False) -> EnergyController:
    _gates(hass, live=live)
    runtime = EnergyRuntime(hass, LocalStateStore(hass, version=1, key=STORAGE_KEY))
    await runtime.async_commit(state)
    device = DeviceIO(hass, call_timeout_s=0.3, readback_timeout_s=0.3, poll_interval_s=0.02)
    return EnergyController(hass, runtime, CONFIG, io=device)


def _charging_on(hass) -> None:
    hass.states.async_set(GRID, "on")
    hass.states.async_set(CES[NIGHT_SLOT], "on")


def _fresh_state():
    return new_installation_state(installation_id="inst-1", policy_version="p",
                                  core_version="c")


# --- provisioning ----------------------------------------------------------------


async def test_unprovisioned_state_only_reconciles_and_writes_nothing(storage_hass, inverter):
    controller = await _controller(storage_hass, _fresh_state(), live=True)
    status = await controller.async_tick()
    assert status.result == "reconcile:required"
    assert status.reason == "not_provisioned"
    assert status.writes == [] and inverter.calls == []


async def test_provision_trusts_counter_then_reconciles_ready(storage_hass, inverter):
    controller = await _controller(storage_hass, _fresh_state())
    await controller.async_provision()
    counter = controller.runtime.state.counter_state
    assert controller.runtime.state.provisioning_state == "ready"
    assert (counter.counter_id, counter.counter_epoch) == (COUNTER, "e1")
    assert counter.last_trusted_raw_dc_kwh == 7204.0
    assert controller.runtime.state.reconcile_state.reason_code == "provisioned"

    status = await controller.async_tick()
    assert controller.runtime.state.reconcile_state.value == "ready"
    assert status.result == "cycle"
    assert inverter.calls == []


async def test_provision_refused_when_counter_is_unknown(storage_hass, inverter):
    storage_hass.states.async_set(COUNTER, "unknown")
    controller = await _controller(storage_hass, _fresh_state())
    with pytest.raises(ProvisionError):
        await controller.async_provision()
    assert controller.runtime.state.provisioning_state == "uninitialized"


async def test_provision_only_once(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state())
    with pytest.raises(ProvisionError):
        await controller.async_provision()


# --- startup reconcile -----------------------------------------------------------


async def test_reconcile_in_shadow_reports_stop_and_writes_nothing(storage_hass, inverter):
    _charging_on(storage_hass)
    state = set_reconcile(empty_state(), "required", "startup")
    controller = await _controller(storage_hass, state, live=False)
    status = await controller.async_tick()
    assert status.reason == "charge_without_grant"
    assert (GRID, False) in [(op.entity_id, op.value) for op in status.intents]
    assert inverter.calls == []
    assert inverter.value(GRID) == "on"
    assert controller.runtime.state.reconcile_state.value == "required"


async def test_reconcile_live_stops_first_then_becomes_ready(storage_hass, inverter):
    _charging_on(storage_hass)
    state = set_reconcile(empty_state(), "required", "startup")
    controller = await _controller(storage_hass, state, live=True)
    await controller.async_tick()
    assert inverter.calls[0] == (GRID, False)
    assert inverter.value(GRID) == "off"
    await controller.async_tick()
    assert controller.runtime.state.reconcile_state.value == "ready"


async def test_readback_unavailable_blocks_cycle(storage_hass, inverter):
    storage_hass.states.async_set(GRID, "unavailable")
    storage_hass.states.async_set(CES[NIGHT_SLOT], "unavailable")
    state = set_reconcile(empty_state(), "required", "startup")
    controller = await _controller(storage_hass, state, live=True)
    status = await controller.async_tick()
    assert status.reason == "readback_unavailable"
    assert controller.runtime.state.reconcile_state.value == "required"


# --- live mode ---------------------------------------------------------------------


async def test_leaving_live_mode_stops_once(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state(), live=True)
    await controller.async_tick()
    _charging_on(storage_hass)  # e.g. someone enabled charging meanwhile
    _gates(storage_hass, live=False)
    status = await controller.async_tick()
    assert status.live is False
    assert inverter.value(GRID) == "off"
    assert all(inverter.value(ce) == "off" for ce in CES)

    _charging_on(storage_hass)
    inverter.calls.clear()
    await controller.async_tick()  # still shadow: no further writes
    assert inverter.calls == []


async def test_release_on_unload_only_after_live(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state(), live=False)
    await controller.async_tick()
    _charging_on(storage_hass)
    await controller.async_release()
    assert inverter.calls == []

    _gates(storage_hass, live=True)
    await controller.async_tick()
    inverter.calls.clear()
    _charging_on(storage_hass)
    await controller.async_release()
    assert inverter.calls[0] == (GRID, False)
    assert inverter.value(GRID) == "off"


async def test_externally_switched_off_grant_goes_through_reconcile(storage_hass, inverter):
    for cap in CAPS:
        storage_hass.states.async_set(cap, "25")
    _charging_on(storage_hass)
    state = set_reconcile(activated(), "ready", None)
    controller = await _controller(storage_hass, state, live=False)
    await controller.async_tick()
    assert controller.runtime.state.active_grant.status == "active"

    storage_hass.states.async_set(GRID, "off")  # outside intervention
    status = await controller.async_tick()
    grant = controller.runtime.state.active_grant
    assert grant.status == "paused"
    assert grant.reason_code == "readback_off"
    assert status.result == "cycle"
    assert status.intents  # shadow: preparation reported, nothing written
    assert inverter.calls == []


async def test_externally_switched_off_grant_is_resumed_in_live_mode(storage_hass, inverter):
    """Current policy: an outside switch-off pauses the grant; the next
    eligible cycle prepares and enables again (the feature switch is the way
    to stop it)."""
    for cap in CAPS:
        storage_hass.states.async_set(cap, "25")
    _charging_on(storage_hass)
    controller = await _controller(storage_hass, set_reconcile(activated(), "ready", None),
                                   live=True)
    storage_hass.states.async_set(GRID, "off")
    status = await controller.async_tick()
    assert status.result == "cycle"
    assert controller.runtime.state.active_grant.status == "active"
    assert inverter.value(GRID) == "on"
    assert inverter.calls[0] == (GRID, False)  # prepared (all permissions off) first


async def test_tick_survives_an_internal_error(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state())
    with patch.object(controller.executor, "async_cycle", side_effect=RuntimeError("boom")):
        status = await controller.async_tick()
    assert status.result == "error" and "boom" in status.error
    assert (await controller.async_tick()).result == "cycle"


async def test_persistence_fault_is_retried_and_requires_reconcile(storage_hass, inverter):
    controller = await _controller(storage_hass, empty_state(), live=True)
    controller.runtime.persistence_fault = "disk"
    status = await controller.async_tick()
    assert controller.runtime.persistence_fault is None
    assert controller.runtime.state.reconcile_state.reason_code is None  # reconciled again
    assert status.result == "cycle"



async def test_wrong_counter_is_fixed_by_reprovision(storage_hass, inverter):
    """A wrong counter entity provisioned first, then corrected in the options:
    the change blocks, and only an explicit reprovision trusts the new one."""
    from custom_components.hallm_energy.lifecycle import provision

    wrong = "sensor.battery_total_discharge"
    storage_hass.states.async_set(wrong, "6842.1")
    state = provision(_fresh_state(), counter_id=wrong, counter_epoch="e1",
                      trusted_raw_dc_kwh=6842.1, at=NOW)
    controller = await _controller(storage_hass, state)
    await controller.async_tick()
    assert controller.runtime.state.reconcile_state.value == "blocked"
    assert controller.runtime.state.reconcile_state.reason_code == "counter_epoch_changed"
    with pytest.raises(ProvisionError):
        await controller.async_provision()

    await controller.async_provision(again=True)
    assert controller.runtime.state.counter_state.counter_id == COUNTER
    assert controller.runtime.state.counter_state.last_trusted_raw_dc_kwh == 7204.0
    await controller.async_tick()
    assert controller.runtime.state.reconcile_state.value == "ready"


async def test_reprovision_refused_while_a_grant_is_active(storage_hass, inverter):
    controller = await _controller(storage_hass, set_reconcile(activated(), "ready", None))
    with pytest.raises(ProvisionError):
        await controller.async_provision(again=True)


# --- recovery in the loop (shadow) -----------------------------------------------------


async def test_shadow_recovery_creates_grant_but_writes_nothing(storage_hass, inverter):
    storage_hass.states.async_set("sensor.bms_soc", "13.0")
    controller = await _controller(storage_hass, empty_state(), live=False)
    status = await controller.async_tick()
    state = controller.runtime.state
    assert status.recovery.action == "grant_prepared"
    assert status.recovery.pv_blocker == "profile_missing"  # no profile: conservative
    assert state.recovery.state == "grid_recovery"
    assert state.active_grant.identity.target_pct == 25
    assert status.intents  # preparation reported
    assert inverter.calls == []


async def test_recovery_waits_for_pv_with_a_usable_profile(storage_hass, inverter):
    import json as _json

    from tests.test_recovery_flow import _forecast, _profile, _sunny

    storage_hass.states.async_set("sensor.bms_soc", "13.0")
    controller = await _controller(storage_hass, empty_state(), live=False)
    controller.profiles.handle(_json.dumps(_profile(now=NOW).to_dict()), NOW)
    controller.pv_forecasts.handle(_json.dumps(_forecast(_sunny, now=NOW).to_dict()), NOW)
    status = await controller.async_tick()
    assert status.recovery.action == "entered" and status.recovery.pv_blocker is None
    assert controller.runtime.state.recovery.state == "wait_pv"
    assert controller.runtime.state.active_grant is None

async def test_configured_budget_factor_reaches_executor_and_reconcile(storage_hass, inverter):
    from custom_components.hallm_energy.config import EnergyConfig
    from tests.test_inputs import OPTIONS

    runtime_config = EnergyConfig.from_options({**OPTIONS, "budget_factor": 1.1})
    controller = await _controller(storage_hass, empty_state())
    rebuilt = EnergyController(storage_hass, controller.runtime, runtime_config, io=controller.io)
    assert rebuilt.executor.budget_factor == 1.1


async def test_local_tomorrow_spans_dst_change(hass):
    from datetime import UTC, datetime

    from custom_components.hallm_energy.controller import _local_tomorrow

    await hass.config.async_set_time_zone("Europe/Prague")
    start, end = _local_tomorrow(datetime(2026, 10, 24, 20, 0, tzinfo=UTC))  # 22:00 CEST
    assert start == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)  # 25. 10. 00:00 CEST
    assert end - start == timedelta(hours=25)  # clocks go back that night


# --- floor table diagnostics and the morning gate (shadow) ----------------------------------


def _d57d_schedule(first_day=27, days=4):
    """D57d (VT 06-07, 09-10, 13-14, 16-17 local) as consecutive intervals."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Prague")
    out = []
    for d in range(days):
        for hour in range(24):
            start = datetime(2026, 10, first_day + d, hour, tzinfo=tz)
            end = start + timedelta(hours=1)
            out.append({"start": start.isoformat(), "end": end.isoformat(),
                        "tariff": "VT" if hour in (6, 9, 13, 16) else "NT"})
    return out


async def test_floor_plan_is_reported_next_to_the_actual_floors_and_never_written(
        storage_hass, inverter):
    from tests.test_inputs import set_telemetry

    await storage_hass.config.async_set_time_zone("Europe/Prague")
    set_telemetry(storage_hass, schedule=_d57d_schedule(), soc="50.0")
    controller = await _controller(storage_hass, empty_state(), live=True)
    status = await controller.async_tick()
    floors = status.floors
    # 23:15 local: VT slots 25, off-peak day slots floor_vt (no profile for the hold),
    # the night slot = the reserve
    assert floors["planned"] == [25, 25, 25, 25, 25, 20]
    assert floors["kinds"] == ["vt", "nt_day", "vt", "mixed", "vt", "nt_night"]
    assert floors["actual"] == [20.0] * 6 and floors["diff"] == [1, 2, 3, 4, 5]
    assert inverter.calls == []  # diagnostics only, even in live mode


async def test_morning_exception_stays_off_by_default(storage_hass, inverter):
    from tests.test_inputs import set_telemetry

    set_telemetry(storage_hass, soc="15.0")
    controller = await _controller(storage_hass, empty_state(), live=False)
    assert controller.config.enable_morning_dip is False
    status = await controller.async_tick()
    assert controller.runtime.state.morning.phase == "none"
    assert status.morning is not None and status.morning.phase == "none"


# --- managed floors in the loop ------------------------------------------------------------


def _managed(controller):
    from custom_components.hallm_energy.config import EnergyConfig
    from tests.test_inputs import OPTIONS

    controller.config = EnergyConfig.from_options({**OPTIONS, "manage_floors": True})
    return controller


async def test_managed_floors_raise_to_the_table_in_live_mode(storage_hass, inverter):
    from tests.test_inputs import set_telemetry

    await storage_hass.config.async_set_time_zone("Europe/Prague")
    set_telemetry(storage_hass, schedule=_d57d_schedule(), soc="50.0")
    controller = _managed(await _controller(storage_hass, empty_state(), live=True))
    status = await controller.async_tick()
    assert status.floors["managed"] is True
    assert [float(inverter.value(c)) for c in CAPS] == [25, 25, 25, 25, 25, 20]
    assert {w.op.entity_id for w in status.writes} == set(CAPS[:5])


async def test_managed_floors_are_lowered_only_with_a_known_schedule(storage_hass, inverter):
    from tests.test_inputs import set_telemetry

    await storage_hass.config.async_set_time_zone("Europe/Prague")
    set_telemetry(storage_hass, schedule=_d57d_schedule(), soc="50.0")
    for cap in CAPS:
        storage_hass.states.async_set(cap, "30.0")
    controller = _managed(await _controller(storage_hass, empty_state(), live=True))
    await controller.async_tick()
    assert [float(inverter.value(c)) for c in CAPS] == [25, 25, 25, 25, 25, 20]  # lowered
    # schedule lost: floors are not lowered (and the plan falls back to floor_vt)
    set_telemetry(storage_hass, soc="50.0")  # default schedule covers only 12 hours
    for cap in CAPS:
        storage_hass.states.async_set(cap, "30.0")
    inverter.calls.clear()
    await controller.async_tick()
    assert [c for c in inverter.calls if c[0] in CAPS] == []


async def test_recovery_entry_gate_depends_on_floor_management(storage_hass, inverter):
    from tests.test_inputs import set_telemetry

    set_telemetry(storage_hass, soc="15.0")
    for cap in CAPS:
        storage_hass.states.async_set(cap, "10.0")  # the old manual morning-dip setting
    plain = await _controller(storage_hass, empty_state(), live=False)
    status = await plain.async_tick()
    assert status.recovery.reason == "floor_below_reserve"
    managed = _managed(await _controller(storage_hass, empty_state(), live=False))
    status = await managed.async_tick()
    assert status.recovery.action == "grant_prepared"  # only the morning phase lowers a floor


async def test_a_persisted_running_phase_ends_on_the_first_tick_when_the_gate_is_off(
        storage_hass, inverter):
    """Restart into 'phase running, gate off': floors must not stay at the dip."""
    from custom_components.hallm_energy.energy_core.morning import MorningState
    from tests.test_inputs import set_telemetry

    set_telemetry(storage_hass, soc="15.0")
    state = empty_state(morning=MorningState("pv_refill", 15.0))
    controller = await _controller(storage_hass, state, live=False)
    assert controller.config.enable_morning_dip is False
    await controller.async_tick()
    assert controller.runtime.state.morning.phase == "none"


async def test_the_grid_relay_decides_between_present_absent_and_unknown(storage_hass, inverter):
    from custom_components.hallm_energy.config import EnergyConfig
    from tests.test_inputs import OPTIONS

    controller = await _controller(storage_hass, empty_state(), live=False)
    assert controller._grid_present() is None  # no relay configured: not known
    controller.config = EnergyConfig.from_options(
        {**OPTIONS, "grid_relay_entity": "binary_sensor.grid"})
    assert controller._grid_present() is False  # configured but entity missing
    for state, expected in (("on", True), ("off", False), ("unavailable", False),
                            ("unknown", False)):
        storage_hass.states.async_set("binary_sensor.grid", state)
        assert controller._grid_present() is expected


async def test_the_evening_of_28_10_a_grant_born_without_data_is_cancelled_by_the_data(
        storage_hass, inverter):
    """The real sequence: restart, SOC below the reserve, no profile/forecast yet -> a
    conservative grant; the snapshots arrive -> it is cancelled, the sun can restore it."""
    import json as _json

    from tests.test_inputs import set_telemetry
    from tests.test_recovery_flow import _forecast, _profile, _sunny

    set_telemetry(storage_hass, soc="14.0")
    controller = await _controller(storage_hass, empty_state(), live=False)
    first = await controller.async_tick()
    assert first.recovery.action == "grant_prepared"
    assert first.recovery.pv_blocker == "profile_missing"
    grant_id = controller.runtime.state.active_grant.identity.grant_id
    controller.profiles.handle(_json.dumps(_profile(now=NOW, hours=60).to_dict()), NOW)
    controller.pv_forecasts.handle(_json.dumps(_forecast(_sunny, now=NOW).to_dict()), NOW)
    second = await controller.async_tick()
    state = controller.runtime.state
    assert second.recovery.action == "grant_cancelled"
    assert state.active_grant is None and state.recovery.state == "wait_pv"
    assert [e.event_id for e in state.pending_audit if e.payload["kind"] == "grant_cancelled"] \
        == [f"{grant_id}:cancelled"]
    assert inverter.calls == []  # shadow: nothing was ever written
