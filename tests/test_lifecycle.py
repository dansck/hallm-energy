"""Pure grant lifecycle over StoreState (no HA)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hallm_energy.energy_core.grant import (
    CounterReading,
    create_recovery_grant,
)
from custom_components.hallm_energy.lifecycle import (
    GrantTransitionError,
    ack_audit,
    confirm_closed,
    lost_state,
    new_installation_state,
    on_startup,
    prepare_grant,
    provision,
    reconcile,
    record_step_result,
    set_reconcile,
    step_grant,
)
from custom_components.hallm_energy.state_schema import (
    CounterState,
    ReconcileState,
    StoreState,
    parse_store_state,
)

T0 = datetime(2026, 10, 27, 22, 0, tzinfo=UTC)
COUNTER = "sensor.battery_total_charge"
C_USE = 46.08


def always_nt(_: datetime) -> bool:
    return True


def never_nt(_: datetime) -> bool:
    return False


def empty_state(**over) -> StoreState:
    state = StoreState(
        state_revision=0,
        policy_version="p1",
        core_version="c1",
        installation_id="inst-1",
        provisioning_state="ready",
        episode_ledger=(),
        active_grant=None,
        counter_state=CounterState(COUNTER, "e1", 7204.0, T0 - timedelta(hours=1)),
        plan_cursor=None,
        pending_step=None,
        reconcile_state=ReconcileState("ready", None),
        pending_audit=(),
    )
    return replace(state, **over)


def recovery_grant(grant_id: str = "g1", *, soc: float = 18.0, hours: int = 6):
    return create_recovery_grant(
        grant_id=grant_id,
        now=T0,
        soc_now_pct=soc,
        reserve_pct=20,
        recovery_target_pct=25,
        c_use_kwh=C_USE,
        counter_id=COUNTER,
        checkpoint_dc_kwh=7204.0,
        window_end=T0 + timedelta(hours=hours),
    )


def prepared(state: StoreState | None = None, grant_id: str = "g1", episode_id: str = "ep1"):
    return prepare_grant(
        state or empty_state(),
        grant=recovery_grant(grant_id),
        episode_id=episode_id,
        counter_epoch="e1",
        now=T0,
        policy_version="p1",
    )


def reading(raw: float, at: datetime) -> CounterReading:
    return CounterReading(counter_id=COUNTER, raw_kwh=raw, at=at)


def step(state: StoreState, **over):
    kwargs = {
        "now": T0 + timedelta(minutes=15),
        "soc_now_pct": 19.0,
        "reading": reading(7204.0, T0 + timedelta(minutes=15)),
        "reading_epoch": "e1",
        "telemetry_valid": True,
        "is_nt": always_nt,
        "deye_prepared": True,
        "safety_revision": 1,
    }
    kwargs.update(over)
    return step_grant(state, **kwargs)


def confirm(result, minutes: int = 16) -> StoreState:
    return record_step_result(
        result.state,
        step_id=result.step_id,
        operation="charge_enable",
        outcome="confirmed",
        now=T0 + timedelta(minutes=minutes),
    )


def activated(state: StoreState | None = None, **over) -> StoreState:
    return confirm(step(state or prepared(), **over))


# --- prepare --------------------------------------------------------------------------


def test_prepare_creates_open_episode_prepared_grant_and_audit():
    state = prepared()
    assert state.active_grant.status == "prepared"
    assert state.active_grant.identity.counter_epoch == "e1"
    assert [(e.episode_id, e.state) for e in state.episode_ledger] == [("ep1", "open")]
    assert [e.event_id for e in state.pending_audit] == ["g1:prepared"]
    assert state.state_revision == 1


def test_prepared_state_survives_store_round_trip():
    state = prepared()
    assert parse_store_state(state.to_dict()) == state


@pytest.mark.parametrize("provisioning", ["uninitialized", "blocked"])
def test_prepare_refused_unless_provisioned(provisioning):
    with pytest.raises(GrantTransitionError):
        prepared(empty_state(provisioning_state=provisioning))


def test_prepare_refused_while_grant_active():
    with pytest.raises(GrantTransitionError):
        prepared(prepared(), grant_id="g2", episode_id="ep2")


def test_prepare_refused_on_counter_epoch_mismatch():
    with pytest.raises(GrantTransitionError):
        prepare_grant(
            empty_state(),
            grant=recovery_grant(),
            episode_id="ep1",
            counter_epoch="e2",
            now=T0,
            policy_version="p1",
        )


def test_prepare_refused_for_empty_window():
    with pytest.raises(GrantTransitionError):
        prepare_grant(
            empty_state(),
            grant=recovery_grant(hours=0),
            episode_id="ep1",
            counter_epoch="e1",
            now=T0,
            policy_version="p1",
        )


# --- enable: proposal, confirmation --------------------------------------------------------


def test_enabling_decision_only_proposes_a_step_and_stays_prepared():
    result = step(prepared())
    assert result.charge_enable is True
    assert result.step_id is not None
    assert result.state.active_grant.status == "prepared"
    assert result.state.pending_step.step_id == result.step_id
    assert result.state.pending_step.operation_status == {}
    assert result.state.pending_step.safety_revision == 1
    assert parse_store_state(result.state.to_dict()) == result.state


def test_confirmed_readback_makes_grant_active_and_clears_step():
    state = activated()
    assert state.active_grant.status == "active"
    assert state.pending_step is None
    assert state.pending_audit[-1].event_id.endswith(":confirmed")


def test_sent_is_not_confirmation():
    result = step(prepared())
    sent = record_step_result(
        result.state, step_id=result.step_id, operation="charge_enable", outcome="sent", now=T0
    )
    assert sent.active_grant.status == "prepared"
    assert sent.pending_step.operation_status == {"charge_enable": "sent"}


def test_failed_enable_leaves_grant_paused_and_not_active():
    result = step(prepared())
    failed = record_step_result(
        result.state, step_id=result.step_id, operation="charge_enable", outcome="failed", now=T0
    )
    assert failed.active_grant.status == "paused"
    assert failed.active_grant.reason_code == "charge_enable_failed"
    assert failed.pending_step is None


def test_unknown_result_keeps_step_requires_reconcile_and_blocks_resend():
    result = step(prepared())
    unknown = record_step_result(
        result.state, step_id=result.step_id, operation="charge_enable", outcome="unknown", now=T0
    )
    assert unknown.active_grant.status == "prepared"
    assert unknown.pending_step.operation_status == {"charge_enable": "unknown"}
    assert unknown.reconcile_state == ReconcileState("required", "step_result_unknown")

    # even once reconcile is ready again, the unresolved step is never resent blindly
    again = step(set_reconcile(unknown, "ready", None))
    assert again.charge_enable is False
    assert again.step_id is None
    assert again.reason == "pending_step_unresolved"


def test_record_result_for_unknown_step_rejected():
    result = step(prepared())
    with pytest.raises(GrantTransitionError):
        record_step_result(
            result.state, step_id="other", operation="charge_enable", outcome="confirmed", now=T0
        )


def test_restart_before_confirmation_never_yields_active():
    # Enable proposed and persisted, crash before the result was recorded.
    result = step(prepared())
    reloaded = on_startup(parse_store_state(result.state.to_dict()))
    assert reloaded.active_grant.status == "prepared"
    assert reloaded.pending_step is not None

    after = step(reloaded, now=T0 + timedelta(minutes=40),
                 reading=reading(7205.0, T0 + timedelta(minutes=40)))
    assert after.charge_enable is False
    assert after.state.active_grant.status == "prepared"
    # usage measured meanwhile is still counted, nothing reset
    assert after.state.active_grant.usage.used_dc_kwh == pytest.approx(1.0)

    # reconcile reads the inverter and records the real outcome
    resolved = record_step_result(
        set_reconcile(after.state, "ready", None),
        step_id=result.step_id,
        operation="charge_enable",
        outcome="confirmed",
        now=T0 + timedelta(minutes=41),
    )
    assert resolved.active_grant.status == "active"


# --- enable blockers ---------------------------------------------------------------


@pytest.mark.parametrize("provisioning", ["uninitialized", "blocked"])
@pytest.mark.parametrize("phase", ["prepared", "active", "paused"])
def test_not_provisioned_never_enables(provisioning, phase):
    state = prepared()
    if phase in ("active", "paused"):
        state = activated(state)
    if phase == "paused":
        state = step(state, is_nt=never_nt).state
        assert state.active_grant.status == "paused"
    blocked = replace(state, provisioning_state=provisioning)
    result = step(blocked, now=T0 + timedelta(minutes=30),
                  reading=reading(7204.5, T0 + timedelta(minutes=30)))
    assert result.charge_enable is False
    assert result.step_id is None
    assert result.reason == f"provisioning_{provisioning}"
    assert result.state.active_grant.status in ("prepared", "paused")
    assert result.state.active_grant.identity == state.active_grant.identity
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(0.5)


def test_not_provisioned_still_honours_soc_stop():
    state = replace(activated(), provisioning_state="blocked")
    result = step(state, soc_now_pct=25.0)
    assert result.state.active_grant.status == "closing"
    assert result.reason == "target_reached"


def test_step_does_not_enable_without_reconcile_ready():
    result = step(set_reconcile(prepared(), "required", "startup"))
    assert result.charge_enable is False
    assert result.reason == "reconcile_not_ready"
    assert result.state.pending_step is None


def test_step_does_not_enable_without_deye_preparation():
    result = step(prepared(), deye_prepared=False)
    assert result.charge_enable is False
    assert result.reason == "deye_not_prepared"


def test_active_grant_stays_on_without_new_write():
    result = step(activated(), now=T0 + timedelta(minutes=30),
                  reading=reading(7204.0, T0 + timedelta(minutes=30)))
    assert result.charge_enable is True
    assert result.step_id is None
    assert result.state.active_grant.status == "active"


# --- pause / close -------------------------------------------------------------------


def test_active_grant_pauses_outside_off_peak_and_keeps_usage():
    active = activated(reading=reading(7205.0, T0 + timedelta(minutes=15)))
    result = step(active, now=T0 + timedelta(minutes=30), is_nt=never_nt,
                  reading=reading(7205.0, T0 + timedelta(minutes=30)))
    assert result.charge_enable is False
    assert result.state.active_grant.status == "paused"
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(1.0)


def test_target_reached_closes_even_with_untrusted_counter():
    result = step(
        activated(),
        soc_now_pct=25.0,
        reading=CounterReading(COUNTER, 0.0, T0 + timedelta(minutes=30), available=False),
    )
    assert result.state.active_grant.status == "closing"
    assert result.reason == "target_reached"
    assert result.charge_enable is False


def test_budget_backup_closes():
    active = activated()
    budget = active.active_grant.identity.budget_dc_kwh
    result = step(
        active,
        now=T0 + timedelta(hours=1),
        reading=reading(7204.0 + budget * 1.25 + 0.1, T0 + timedelta(hours=1)),
    )
    assert result.state.active_grant.status == "closing"
    assert result.reason == "budget_exhausted"


def test_counter_decrease_never_lowers_usage():
    active = activated(reading=reading(7205.0, T0 + timedelta(minutes=15)))
    result = step(active, now=T0 + timedelta(minutes=30),
                  reading=reading(7204.2, T0 + timedelta(minutes=30)))
    assert result.charge_enable is False
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(1.0)


def test_counter_epoch_change_blocks_and_preserves_checkpoint_and_usage():
    active = activated(reading=reading(7205.0, T0 + timedelta(minutes=15)))
    result = step(active, reading_epoch="e2", reading=reading(0.4, T0 + timedelta(minutes=30)))
    assert result.charge_enable is False
    assert result.state.reconcile_state == ReconcileState("blocked", "counter_epoch_changed")
    assert result.state.active_grant.status == "paused"
    assert result.state.active_grant.identity.checkpoint_dc_kwh == 7204.0
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(1.0)


def test_unchanged_poll_does_not_bump_revision():
    active = activated()
    first = step(active, now=T0 + timedelta(minutes=30),
                 reading=reading(7204.0, T0 + timedelta(minutes=30)))
    again = step(first.state, now=T0 + timedelta(minutes=45),
                 reading=reading(7204.0, T0 + timedelta(minutes=45)))
    assert again.state is first.state


def test_closing_grant_never_re_enables_charge():
    closing = step(activated(), soc_now_pct=25.0).state
    result = step(closing, soc_now_pct=10.0)
    assert result.charge_enable is False
    assert result.state is closing


# --- close ----------------------------------------------------------------------------


def test_confirm_closed_is_atomic_ledger_usage_and_audit():
    active = activated(reading=reading(7205.0, T0 + timedelta(minutes=15)))
    closing = step(active, soc_now_pct=25.0, reading=reading(7205.0, T0 + timedelta(minutes=30)))
    closed = confirm_closed(closing.state, now=T0 + timedelta(minutes=31))
    assert closed.active_grant is None
    (entry,) = closed.episode_ledger
    assert entry.state == "closed"
    assert entry.close_reason == "target_reached"
    assert entry.final_used_dc_kwh == pytest.approx(1.0)
    kinds = [e.payload["kind"] for e in closed.pending_audit]
    assert kinds == ["grant_prepared", "step_confirmed", "grant_closing", "grant_closed"]
    assert parse_store_state(closed.to_dict()) == closed


def test_confirm_closed_requires_closing():
    with pytest.raises(GrantTransitionError):
        confirm_closed(prepared(), now=T0)


def test_confirm_closed_refused_with_unresolved_step():
    proposed = step(prepared())
    unknown = record_step_result(
        proposed.state, step_id=proposed.step_id, operation="charge_enable",
        outcome="unknown", now=T0,
    )
    closing = step(set_reconcile(unknown, "ready", None), soc_now_pct=25.0).state
    assert closing.active_grant.status == "closing"
    with pytest.raises(GrantTransitionError):
        confirm_closed(closing, now=T0 + timedelta(hours=1))


def test_closed_episode_cannot_be_replayed():
    closed = confirm_closed(step(activated(), soc_now_pct=25.0).state, now=T0 + timedelta(hours=1))
    with pytest.raises(GrantTransitionError):
        prepared(closed, grant_id="g2", episode_id="ep1")
    with pytest.raises(GrantTransitionError):
        prepared(closed, grant_id="g1", episode_id="ep2")


def test_new_episode_allowed_after_documented_close():
    closed = confirm_closed(step(activated(), soc_now_pct=25.0).state, now=T0 + timedelta(hours=1))
    again = prepared(closed, grant_id="g2", episode_id="ep2")
    assert again.active_grant.identity.grant_id == "g2"
    assert [e.state for e in again.episode_ledger] == ["closed", "open"]


# --- restart --------------------------------------------------------------------------


def test_restart_mid_grant_requires_reconcile_and_keeps_remaining_claim():
    active = activated(reading=reading(7205.0, T0 + timedelta(minutes=15)))
    reloaded = on_startup(parse_store_state(active.to_dict()))
    assert reloaded.reconcile_state.value == "required"

    blocked = step(reloaded, now=T0 + timedelta(minutes=40),
                   reading=reading(7206.0, T0 + timedelta(minutes=40)))
    assert blocked.charge_enable is False
    assert blocked.state.active_grant.status == "paused"
    assert blocked.state.active_grant.usage.used_dc_kwh == pytest.approx(2.0)  # not reset

    ready = set_reconcile(blocked.state, "ready", None)
    resumed = step(ready, now=T0 + timedelta(minutes=45),
                   reading=reading(7206.0, T0 + timedelta(minutes=45)))
    assert resumed.charge_enable is True
    assert resumed.state.active_grant.status == "paused"  # proposal, not yet confirmed
    assert resumed.state.active_grant.identity.budget_dc_kwh == (
        active.active_grant.identity.budget_dc_kwh
    )


# --- bootstrap / provisioning ---------------------------------------------------------

def _fresh():
    return new_installation_state(installation_id="inst-1", policy_version="p1", core_version="c1")


def test_first_run_state_cannot_prepare_a_grant():
    with pytest.raises(GrantTransitionError):
        prepared(_fresh())


def test_provision_requires_reconcile_before_anything_enables():
    state = provision(_fresh(), counter_id=COUNTER, counter_epoch="e1",
                      trusted_raw_dc_kwh=7204.0, at=T0)
    assert state.provisioning_state == "ready"
    assert state.reconcile_state == ReconcileState("required", "provisioned")
    result = step(prepared(state))
    assert result.charge_enable is False
    assert result.reason == "reconcile_not_ready"


def test_lost_state_cannot_be_provisioned_or_grant_recreated():
    lost = lost_state(installation_id="inst-1", policy_version="p1", core_version="c1",
                      reason="store_lost")
    with pytest.raises(GrantTransitionError):
        provision(lost, counter_id=COUNTER, counter_epoch="e1", trusted_raw_dc_kwh=0.0, at=T0)
    with pytest.raises(GrantTransitionError):
        prepared(lost)


def test_startup_keeps_blocked():
    blocked = set_reconcile(prepared(), "blocked", "counter_epoch_changed")
    assert on_startup(blocked) is blocked


def test_ack_audit_drops_only_acknowledged_events():
    state = step(prepared(), soc_now_pct=25.0).state  # prepared + closing events
    acked = ack_audit(state, {"g1:prepared"})
    assert [e.event_id for e in acked.pending_audit] == ["g1:closing"]
    assert ack_audit(acked, {"nope"}) is acked


# --- reconcile ------------------------------------------------------------------------


def rec(state, *, on, raw=7204.0, epoch="e1", minutes=30, available=True):
    at = T0 + timedelta(minutes=minutes)
    return reconcile(
        state,
        now=at,
        observed_charge_enable=on,
        reading=CounterReading(COUNTER, raw, at, available=available),
        reading_epoch=epoch,
    )


def test_reconcile_confirms_pending_enable_from_readback_on():
    proposed = step(prepared()).state
    result = rec(on_startup(proposed), on=True)
    assert result.reason is None
    assert result.require_charge_off is False
    assert result.state.reconcile_state.value == "ready"
    assert result.state.active_grant.status == "active"
    assert result.state.pending_step is None


def test_reconcile_marks_pending_enable_failed_from_readback_off():
    proposed = step(prepared()).state
    result = rec(on_startup(proposed), on=False)
    assert result.state.reconcile_state.value == "ready"
    assert result.state.active_grant.status == "paused"
    assert result.state.pending_step is None


def test_reconcile_counts_charging_during_downtime_through_checkpoint():
    active = activated()
    result = rec(on_startup(active), on=True, raw=7206.5, minutes=60)
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(2.5)
    assert result.state.active_grant.identity.budget_dc_kwh == (
        active.active_grant.identity.budget_dc_kwh
    )


def test_reconcile_pauses_active_grant_when_readback_off():
    result = rec(on_startup(activated()), on=False)
    assert result.state.active_grant.status == "paused"
    assert result.state.active_grant.reason_code == "readback_off"
    assert result.state.reconcile_state.value == "ready"


def test_reconcile_unexpected_charge_requires_off_and_not_ready():
    result = rec(on_startup(prepared()), on=True)
    assert result.require_charge_off is True
    assert result.reason == "unexpected_charge"
    assert result.state.reconcile_state.value == "required"


def test_reconcile_charge_without_grant_requires_off():
    closed = confirm_closed(step(activated(), soc_now_pct=25.0).state, now=T0 + timedelta(hours=1))
    result = rec(on_startup(closed), on=True, minutes=90)
    assert result.require_charge_off is True
    assert result.reason == "charge_without_grant"


def test_reconcile_closes_closing_grant_once_off_is_confirmed():
    closing = step(activated(), soc_now_pct=25.0).state
    still_on = rec(on_startup(closing), on=True)
    assert still_on.require_charge_off is True
    assert still_on.state.active_grant.status == "closing"
    off = rec(still_on.state, on=False, minutes=31)
    assert off.state.active_grant is None
    assert off.state.episode_ledger[-1].state == "closed"
    assert off.state.reconcile_state.value == "ready"


def test_reconcile_counter_decrease_blocks_and_preserves_usage():
    active = step(activated(), now=T0 + timedelta(minutes=30),
                  reading=reading(7205.0, T0 + timedelta(minutes=30))).state
    result = rec(on_startup(active), on=True, raw=12.0, minutes=60)
    assert result.state.reconcile_state == ReconcileState("blocked", "counter_decreased")
    assert result.require_charge_off is True
    assert result.state.active_grant.status == "paused"
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(1.0)
    assert result.state.active_grant.identity.checkpoint_dc_kwh == 7204.0


def test_reconcile_counter_epoch_change_blocks():
    result = rec(on_startup(activated()), on=False, epoch="e2")
    assert result.state.reconcile_state == ReconcileState("blocked", "counter_epoch_changed")


def test_reconcile_never_leaves_blocked_by_itself():
    blocked = rec(on_startup(activated()), on=False, epoch="e2").state
    again = rec(blocked, on=False)
    assert again.state is blocked
    assert again.state.reconcile_state.value == "blocked"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"on": None}, "readback_unavailable"),
        ({"on": False, "available": False}, "counter_unavailable"),
    ],
)
def test_reconcile_without_fresh_inputs_stays_required(kwargs, reason):
    result = rec(on_startup(activated()), **kwargs)
    assert result.reason == reason
    assert result.state.reconcile_state.value == "required"


def test_after_reconcile_ready_enabling_still_goes_through_a_step():
    reconciled = rec(on_startup(step(prepared(), is_nt=never_nt).state), on=False).state
    assert reconciled.reconcile_state.value == "ready"
    result = step(reconciled, now=T0 + timedelta(minutes=45),
                  reading=reading(7204.0, T0 + timedelta(minutes=45)))
    assert result.step_id is not None
    assert result.state.active_grant.status == "prepared"
