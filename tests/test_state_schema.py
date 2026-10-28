"""Store state schema v1 — validation and serialization (pure, no HA)."""

from __future__ import annotations

import copy
import json
import math

import pytest

from custom_components.hallm_energy.state_schema import (
    StoreStateError,
    parse_store_state,
)

T0 = 1_761_550_000


def _grant() -> dict:
    return {
        "identity": {
            "grant_id": "g1",
            "episode_id": "e1",
            "purpose": "recovery",
            "created_at": T0,
            "target_pct": 25.0,
            "budget_dc_kwh": 3.25,
            "counter_id": "sensor.battery_total_charge",
            "counter_epoch": "c1",
            "checkpoint_dc_kwh": 7204.0,
        },
        "window": {"start": T0, "end": T0 + 6 * 3600},
        "status": "prepared",
        "reason_code": None,
        "status_changed_at": T0,
        "usage": {
            "used_dc_kwh": 0.0,
            "counter_changed_at": None,
            "last_trusted_raw_dc_kwh": 7204.0,
        },
        "created_policy_version": "p1",
        "plan_ref": None,
    }


def _raw(**over) -> dict:
    raw = {
        "state_revision": 3,
        "policy_version": "p1",
        "core_version": "c1",
        "installation_id": "inst-1",
        "provisioning_state": "ready",
        "episode_ledger": [
            {
                "episode_id": "e0",
                "purpose": "recovery",
                "state": "closed",
                "original_grant_id": "g0",
                "original_budget_dc_kwh": 2.0,
                "final_used_dc_kwh": 2.4,
                "close_reason": "target_reached",
                "close_at": T0 - 86400,
                "deadline_at": None,
            },
            {
                "episode_id": "e1",
                "purpose": "recovery",
                "state": "open",
                "original_grant_id": "g1",
                "original_budget_dc_kwh": 3.25,
                "final_used_dc_kwh": None,
                "close_reason": None,
                "close_at": None,
                "deadline_at": T0 + 36 * 3600,
            },
        ],
        "active_grant": _grant(),
        "counter_state": {
            "counter_id": "sensor.battery_total_charge",
            "counter_epoch": "c1",
            "last_trusted_raw_dc_kwh": 7204.0,
            "last_changed_at": T0,
        },
        "plan_cursor": {"producer_id": "planner", "last_accepted_epoch": 42},
        "pending_step": None,
        "reconcile_state": {"value": "required", "reason_code": "startup"},
        "pending_audit": [{"event_id": "ev1", "payload": {"kind": "grant_prepared"}}],
    }
    raw.update(over)
    return raw


def test_valid_snapshot_round_trips_through_json():
    state = parse_store_state(_raw())
    serialized = json.loads(json.dumps(state.to_dict()))
    assert parse_store_state(serialized) == state
    # files written before `recovery` existed parse unchanged; new writes carry the key
    expected = _raw()
    expected["pending_audit"] = [{**e, "created_at": None} for e in expected["pending_audit"]]
    assert serialized == {**expected, "recovery": None,
                          "morning": {"phase": "none", "floor_state_pct": None}}


def test_real_payload_exceeds_input_text_limit_and_still_round_trips():
    state = parse_store_state(_raw())
    assert len(json.dumps(state.to_dict(), separators=(",", ":"))) > 255
    assert parse_store_state(state.to_dict()) == state


def test_empty_state_without_grant_is_valid():
    state = parse_store_state(
        _raw(active_grant=None, episode_ledger=[], plan_cursor=None, pending_audit=[])
    )
    assert state.active_grant is None


@pytest.mark.parametrize(
    "field",
    [
        "state_revision",
        "policy_version",
        "core_version",
        "installation_id",
        "provisioning_state",
        "episode_ledger",
        "counter_state",
        "reconcile_state",
        "pending_audit",
    ],
)
def test_missing_required_root_field_rejected(field):
    raw = _raw()
    del raw[field]
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


@pytest.mark.parametrize(
    "field", ["grant_id", "episode_id", "counter_id", "counter_epoch", "checkpoint_dc_kwh"]
)
def test_missing_grant_identity_field_is_not_defaulted(field):
    raw = _raw()
    del raw["active_grant"]["identity"][field]
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_missing_used_dc_kwh_is_not_defaulted_to_zero():
    raw = _raw()
    del raw["active_grant"]["usage"]["used_dc_kwh"]
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


@pytest.mark.parametrize("bad", [True, "1.0", math.nan, math.inf, -0.5])
def test_invalid_energy_values_rejected(bad):
    raw = _raw()
    raw["active_grant"]["usage"]["used_dc_kwh"] = bad
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_bool_state_revision_rejected():
    with pytest.raises(StoreStateError):
        parse_store_state(_raw(state_revision=True))


def test_negative_state_revision_rejected():
    with pytest.raises(StoreStateError):
        parse_store_state(_raw(state_revision=-1))


def test_used_above_budget_is_kept_not_clipped():
    raw = _raw()
    raw["active_grant"]["usage"]["used_dc_kwh"] = 4.0  # > budget 3.25, < 1.25 * budget
    raw["active_grant"]["usage"]["last_trusted_raw_dc_kwh"] = 7208.0
    state = parse_store_state(raw)
    assert state.active_grant.usage.used_dc_kwh == 4.0
    assert state.active_grant.identity.budget_dc_kwh == 3.25


@pytest.mark.parametrize(
    "path,value",
    [
        (("provisioning_state",), "maybe"),
        (("reconcile_state", "value"), "done"),
        (("active_grant", "status"), "running"),
        (("active_grant", "identity", "purpose"), "arbitrage"),
    ],
)
def test_unknown_enum_values_rejected(path, value):
    raw = _raw()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_window_end_must_be_after_start():
    raw = _raw()
    raw["active_grant"]["window"] = {"start": T0, "end": T0}
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_grant_counter_must_match_counter_state():
    raw = _raw()
    raw["counter_state"]["counter_epoch"] = "c2"
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_grant_without_ledger_episode_rejected():
    raw = _raw()
    raw["episode_ledger"] = [e for e in raw["episode_ledger"] if e["episode_id"] != "e1"]
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_grant_pointing_at_closed_episode_rejected():
    raw = _raw()
    raw["active_grant"]["identity"]["episode_id"] = "e0"
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_closed_episode_requires_final_usage_and_close_time():
    raw = _raw()
    raw["episode_ledger"][0]["final_used_dc_kwh"] = None
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_pending_step_round_trips():
    step = {
        "step_id": "s1",
        "grant_id": "g1",
        "episode_id": "e1",
        "observed_before": {"time_point_2_charge_enable": "off"},
        "requested_changes": {"time_point_2_charge_enable": "on"},
        "operation_order": ["prepare_slots", "charge_enable"],
        "safety_revision": 7,
        "operation_status": {"prepare_slots": "confirmed", "charge_enable": "unknown"},
    }
    raw = _raw(pending_step=step)
    state = parse_store_state(raw)
    assert parse_store_state(state.to_dict()) == state
    assert state.pending_step.operation_status["charge_enable"] == "unknown"


def test_pending_step_unknown_operation_rejected():
    step = {
        "step_id": "s1",
        "grant_id": "g1",
        "episode_id": "e1",
        "observed_before": {},
        "requested_changes": {},
        "operation_order": ["charge_enable"],
        "safety_revision": 1,
        "operation_status": {"something_else": "sent"},
    }
    with pytest.raises(StoreStateError):
        parse_store_state(_raw(pending_step=step))


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_dc_kwh", 100.0),  # claim inflated against the ledger
        ("grant_id", "g-other"),
        ("purpose", "econ"),
    ],
)
def test_grant_identity_must_match_its_ledger_episode(field, value):
    raw = _raw()
    raw["active_grant"]["identity"][field] = value
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


@pytest.mark.parametrize("target", [150.0, -1.0, 100.1])
def test_target_outside_physical_soc_range_rejected(target):
    raw = _raw()
    raw["active_grant"]["identity"]["target_pct"] = target
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_target_at_100_accepted():
    raw = _raw()
    raw["active_grant"]["identity"]["target_pct"] = 100.0
    assert parse_store_state(raw).active_grant.identity.target_pct == 100.0


def test_duplicate_episode_id_in_ledger_rejected():
    raw = _raw()
    raw["episode_ledger"][0]["episode_id"] = "e1"
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_duplicate_grant_id_in_ledger_rejected():
    raw = _raw()
    raw["episode_ledger"][0]["original_grant_id"] = "g1"
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_two_open_episodes_rejected():
    raw = _raw()
    raw["episode_ledger"][0].update(
        state="open", final_used_dc_kwh=None, close_reason=None, close_at=None
    )
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_open_episode_without_active_grant_rejected():
    with pytest.raises(StoreStateError):
        parse_store_state(_raw(active_grant=None))


def test_usage_inconsistent_with_counter_rejected():
    raw = _raw()
    raw["active_grant"]["usage"]["used_dc_kwh"] = 2.0  # raw still at checkpoint
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_usage_within_counter_step_tolerance_accepted():
    raw = _raw()
    raw["active_grant"]["usage"]["used_dc_kwh"] = 1.0
    raw["active_grant"]["usage"]["last_trusted_raw_dc_kwh"] = 7205.05
    assert parse_store_state(raw).active_grant.usage.used_dc_kwh == 1.0


def test_trusted_raw_below_checkpoint_rejected():
    raw = _raw()
    raw["active_grant"]["usage"]["last_trusted_raw_dc_kwh"] = 7200.0
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def test_pending_step_for_another_grant_rejected():
    step = {
        "step_id": "s1",
        "grant_id": "g-other",
        "episode_id": "e1",
        "observed_before": {},
        "requested_changes": {},
        "operation_order": ["charge_enable"],
        "safety_revision": 1,
        "operation_status": {},
    }
    with pytest.raises(StoreStateError):
        parse_store_state(_raw(pending_step=step))


def test_parse_does_not_mutate_input():
    raw = _raw()
    snapshot = copy.deepcopy(raw)
    parse_store_state(raw)
    assert raw == snapshot


def test_non_mapping_root_rejected():
    with pytest.raises(StoreStateError):
        parse_store_state([])


# --- recovery episode ---------------------------------------------------------------


def _recovery(**over) -> dict:
    raw = {"episode_id": "e1", "entered_at": T0, "wait_until": T0 + 36 * 3600,
           "state": "grid_recovery", "decided_at": T0, "reason_code": None}
    raw.update(over)
    return raw


def test_grid_recovery_episode_with_its_grant_round_trips():
    state = parse_store_state(_raw(recovery=_recovery()))
    assert state.recovery.state == "grid_recovery"
    assert parse_store_state(json.loads(json.dumps(state.to_dict()))) == state


def test_wait_pv_episode_without_grant_is_valid():
    raw = _raw(active_grant=None, recovery=_recovery(episode_id="e2", state="wait_pv"))
    raw["episode_ledger"] = raw["episode_ledger"][:1]
    assert parse_store_state(raw).recovery.state == "wait_pv"


@pytest.mark.parametrize(("over", "ledger_keep"), [
    ({"recovery": _recovery(state="wait_pv")}, 2),                       # wait_pv + grant
    ({"active_grant": None, "recovery": _recovery(episode_id="e2")}, 1),  # grid without grant
    ({"recovery": _recovery(episode_id="other")}, 2),                    # grant of another episode
    ({"active_grant": None, "recovery": _recovery(episode_id="e0", state="wait_pv")}, 1),  # closed
    ({"recovery": _recovery(wait_until=T0)}, 2),                         # deadline not after entry
    ({"recovery": _recovery(state="waiting")}, 2),
])
def test_inconsistent_recovery_is_rejected(over, ledger_keep):
    raw = _raw(**over)
    raw["episode_ledger"] = raw["episode_ledger"][:ledger_keep]
    with pytest.raises(StoreStateError):
        parse_store_state(raw)


def _closed_raw(**rec):
    raw = _raw(active_grant=None, recovery=_recovery(**rec))
    raw["episode_ledger"] = raw["episode_ledger"][:1]
    raw["episode_ledger"][0] = {**raw["episode_ledger"][0], "episode_id": "e1"}
    return raw


def test_after_grant_states_need_the_closed_episode():
    state = parse_store_state(_closed_raw(state="cloudy_hold", restore_floors=[20] * 6,
                                          hold_floor_pct=25))
    assert state.recovery.restore_floors == (20.0,) * 6
    assert state.recovery.hold_floor_pct == 25
    assert parse_store_state(_closed_raw(state="restoring", restore_floors=[20] * 6,
                                         hold_floor_pct=25)) is not None
    with pytest.raises(StoreStateError):  # restoring without the hold floor
        parse_store_state(_closed_raw(state="restoring"))
    with pytest.raises(StoreStateError):
        parse_store_state(_closed_raw(state="restoring", restore_floors=[20] * 6))
    # floor management does not need saved floors: restoring with only the hold floor
    assert parse_store_state(_closed_raw(state="restoring", hold_floor_pct=25)) is not None
    with pytest.raises(StoreStateError):  # hold while the grant is still open
        parse_store_state(_raw(recovery=_recovery(state="cloudy_hold")))
    with pytest.raises(StoreStateError):
        parse_store_state(_closed_raw(state="cloudy_hold", restore_floors=[150] * 6))


# --- morning exception state ---------------------------------------------------------


def test_morning_state_round_trips_and_old_files_default_to_none():
    state = parse_store_state(_raw(active_grant=None, episode_ledger=[],
                                   morning={"phase": "pv_refill", "floor_state_pct": 15}))
    assert state.morning.phase == "pv_refill" and state.morning.floor_state_pct == 15
    assert parse_store_state(json.loads(json.dumps(state.to_dict()))) == state
    assert parse_store_state(_raw()).morning.phase == "none"  # key missing in old files


@pytest.mark.parametrize("morning", [
    {"phase": "pv_refill", "floor_state_pct": None},   # refill needs its floor
    {"phase": "pv_refill", "floor_state_pct": 150},
    {"phase": "morning_dip", "floor_state_pct": 15},   # floor only in refill
    {"phase": "dawn", "floor_state_pct": None},
])
def test_inconsistent_morning_state_is_rejected(morning):
    with pytest.raises(StoreStateError):
        parse_store_state(_raw(active_grant=None, episode_ledger=[], morning=morning))


def test_morning_phase_cannot_run_with_a_grant():
    with pytest.raises(StoreStateError):
        parse_store_state(_raw(morning={"phase": "morning_dip", "floor_state_pct": None}))
