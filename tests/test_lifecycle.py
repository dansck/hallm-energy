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
    confirm_closed,
    on_startup,
    prepare_grant,
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
    }
    kwargs.update(over)
    return step_grant(state, **kwargs)


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


@pytest.mark.parametrize(
    "state",
    [
        empty_state(provisioning_state="uninitialized"),
        empty_state(provisioning_state="blocked"),
    ],
)
def test_prepare_refused_unless_provisioned(state):
    with pytest.raises(GrantTransitionError):
        prepared(state)


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


# --- step -----------------------------------------------------------------------------


def test_step_activates_when_all_conditions_hold():
    result = step(prepared())
    assert result.charge_enable is True
    assert result.state.active_grant.status == "active"


def test_step_does_not_activate_without_reconcile_ready():
    state = set_reconcile(prepared(), "required", "startup")
    result = step(state)
    assert result.charge_enable is False
    assert result.reason == "reconcile_not_ready"
    assert result.state.active_grant.status == "prepared"


def test_step_does_not_activate_without_deye_preparation():
    result = step(prepared(), deye_prepared=False)
    assert result.charge_enable is False
    assert result.reason == "deye_not_prepared"


def test_active_grant_pauses_outside_off_peak_and_keeps_usage():
    active = step(prepared(), reading=reading(7205.0, T0 + timedelta(minutes=15))).state
    result = step(active, now=T0 + timedelta(minutes=30), is_nt=never_nt,
                  reading=reading(7205.0, T0 + timedelta(minutes=30)))
    assert result.charge_enable is False
    assert result.state.active_grant.status == "paused"
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(1.0)


def test_target_reached_closes_even_with_untrusted_counter():
    active = step(prepared()).state
    result = step(
        active,
        soc_now_pct=25.0,
        reading=CounterReading(COUNTER, 0.0, T0 + timedelta(minutes=30), available=False),
    )
    assert result.state.active_grant.status == "closing"
    assert result.reason == "target_reached"
    assert result.charge_enable is False


def test_budget_backup_closes():
    active = step(prepared()).state
    budget = active.active_grant.identity.budget_dc_kwh
    result = step(
        active,
        now=T0 + timedelta(hours=1),
        reading=reading(7204.0 + budget * 1.25 + 0.1, T0 + timedelta(hours=1)),
    )
    assert result.state.active_grant.status == "closing"
    assert result.reason == "budget_exhausted"


def test_counter_decrease_never_lowers_usage():
    active = step(prepared(), reading=reading(7205.0, T0 + timedelta(minutes=15))).state
    result = step(active, now=T0 + timedelta(minutes=30),
                  reading=reading(7204.2, T0 + timedelta(minutes=30)))
    assert result.charge_enable is False
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(1.0)


def test_counter_epoch_change_blocks_and_preserves_checkpoint_and_usage():
    active = step(prepared(), reading=reading(7205.0, T0 + timedelta(minutes=15))).state
    result = step(active, reading_epoch="e2", reading=reading(0.4, T0 + timedelta(minutes=30)))
    assert result.charge_enable is False
    assert result.state.reconcile_state == ReconcileState("blocked", "counter_epoch_changed")
    assert result.state.active_grant.status == "paused"
    assert result.state.active_grant.identity.checkpoint_dc_kwh == 7204.0
    assert result.state.active_grant.usage.used_dc_kwh == pytest.approx(1.0)


def test_unchanged_poll_does_not_bump_revision():
    active = step(prepared()).state
    again = step(active)
    assert again.state is active


def test_closing_grant_never_re_enables_charge():
    closing = step(step(prepared()).state, soc_now_pct=25.0).state
    result = step(closing, soc_now_pct=10.0)
    assert result.charge_enable is False
    assert result.state is closing


# --- close ----------------------------------------------------------------------------


def test_confirm_closed_is_atomic_ledger_usage_and_audit():
    closing = step(step(prepared(), reading=reading(7205.0, T0 + timedelta(minutes=15))).state,
                   soc_now_pct=25.0, reading=reading(7205.0, T0 + timedelta(minutes=30))).state
    closed = confirm_closed(closing, now=T0 + timedelta(minutes=31))
    assert closed.active_grant is None
    (entry,) = closed.episode_ledger
    assert entry.state == "closed"
    assert entry.close_reason == "target_reached"
    assert entry.final_used_dc_kwh == pytest.approx(1.0)
    assert [e.event_id for e in closed.pending_audit] == ["g1:prepared", "g1:closing", "g1:closed"]
    assert parse_store_state(closed.to_dict()) == closed


def test_confirm_closed_requires_closing():
    with pytest.raises(GrantTransitionError):
        confirm_closed(prepared(), now=T0)


def test_closed_episode_cannot_be_replayed():
    closing = step(step(prepared()).state, soc_now_pct=25.0).state
    closed = confirm_closed(closing, now=T0 + timedelta(hours=1))
    with pytest.raises(GrantTransitionError):
        prepared(closed, grant_id="g2", episode_id="ep1")
    with pytest.raises(GrantTransitionError):
        prepared(closed, grant_id="g1", episode_id="ep2")


def test_new_episode_allowed_after_documented_close():
    closing = step(step(prepared()).state, soc_now_pct=25.0).state
    closed = confirm_closed(closing, now=T0 + timedelta(hours=1))
    again = prepared(closed, grant_id="g2", episode_id="ep2")
    assert again.active_grant.identity.grant_id == "g2"
    assert [e.state for e in again.episode_ledger] == ["closed", "open"]


# --- restart --------------------------------------------------------------------------


def test_restart_mid_grant_requires_reconcile_and_keeps_remaining_claim():
    # Charging may have run before the crash; after restart the stored state is
    # reloaded, reconcile is required, and nothing re-enables charge until ready.
    active = step(prepared(), reading=reading(7205.0, T0 + timedelta(minutes=15))).state
    reloaded = on_startup(parse_store_state(active.to_dict()))
    assert reloaded.reconcile_state.value == "required"

    blocked = step(reloaded, now=T0 + timedelta(minutes=40),
                   reading=reading(7206.0, T0 + timedelta(minutes=40)))
    assert blocked.charge_enable is False
    assert blocked.state.active_grant.usage.used_dc_kwh == pytest.approx(2.0)  # not reset

    ready = set_reconcile(blocked.state, "ready", None)
    resumed = step(ready, now=T0 + timedelta(minutes=45),
                   reading=reading(7206.0, T0 + timedelta(minutes=45)))
    assert resumed.charge_enable is True
    assert resumed.state.active_grant.identity.budget_dc_kwh == (
        active.active_grant.identity.budget_dc_kwh
    )
