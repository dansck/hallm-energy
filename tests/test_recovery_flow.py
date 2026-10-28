"""Recovery episodes with synthetic profiles (pure): wait for PV only with a
usable profile, anchored deadline, one grant per episode, window extension,
end of episode, restart without a new budget."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hallm_energy.energy_core.grant import CounterReading
from custom_components.hallm_energy.energy_core.house_load import (
    PROFILE_STEP,
    HouseLoadProfile,
    ProfileQuality,
    ProfileStep,
)
from custom_components.hallm_energy.lifecycle import confirm_closed, prepare_grant, step_grant
from custom_components.hallm_energy.profile_receiver import ProfileReceiver
from custom_components.hallm_energy.recovery_flow import (
    RecoveryInputs,
    RecoveryParams,
    plan_recovery,
)
from custom_components.hallm_energy.state_schema import parse_store_state
from tests.test_lifecycle import COUNTER, empty_state

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)
PARAMS = RecoveryParams(reserve_pct=20, recovery_target_pct=25, recovery_pv_wait_h=36,
                        c_use_kwh=46.08, eta_c=0.95, eta_d=0.95, e_err_morning_kwh=2.0,
                        assumed_charge_rate_dc_kw=1.0, topology_epoch=1)


def _profile(*, now=NOW, kw=1.0, status="ok", season="ok", epoch=1, hours=48,
             generated=None):
    generated = generated or now - timedelta(minutes=5)
    start = generated.replace(minute=0, second=0, microsecond=0)
    steps = tuple(ProfileStep(start + i * PROFILE_STEP, kw * 0.8, kw, kw * 1.5)
                  for i in range(hours * 4))
    return HouseLoadProfile(
        profile_id="p", model_name="m", model_version="v", topology_epoch=epoch,
        generated_at=generated, valid_until=generated + timedelta(hours=3),
        horizon_start=start, horizon_end=start + timedelta(hours=hours),
        quality=ProfileQuality(status=status, history_start=generated - timedelta(days=28),
                               history_end=generated, days_used=28, min_samples_per_step=7,
                               history_quarters_expected=2688, history_quarters_used=2680,
                               season_coverage=season),
        steps=steps,
    )


def _sunny(t):  # 6 kW AC 08:00-14:00 UTC each day, else nothing
    return 6.0 if 8 <= t.hour < 14 else 0.0


def _nt_steps(now=NOW, count=32):
    return tuple(now + timedelta(hours=2) + i * PROFILE_STEP for i in range(count))


def _inputs(*, now=NOW, soc=13.0, floor=20.0, pv=_sunny, profile="default", steps=None,
            raw=7204.0, available=True):
    return RecoveryInputs(
        now=now, soc_pct=soc, current_floor_pct=floor,
        reading=CounterReading(COUNTER, raw, now, available=available), reading_epoch="e1",
        pv_ac_kw=pv, profile=_profile(now=now) if profile == "default" else profile,
        nt_steps=_nt_steps(now) if steps is None else steps,
    )


_ids = iter(f"id{i}" for i in range(10_000))


def _plan(state, inputs, params=PARAMS):
    return plan_recovery(state, inputs, params, new_id=lambda: next(_ids), policy_version="p")


# --- entry -----------------------------------------------------------------------------


def test_usable_profile_and_enough_sun_waits_and_buys_nothing():
    state, outcome = _plan(empty_state(), _inputs())
    assert outcome.action == "entered" and outcome.pv_blocker is None
    assert state.recovery.state == "wait_pv"
    assert state.recovery.wait_until == NOW + timedelta(hours=36)
    assert state.active_grant is None


def test_no_episode_while_floor_allows_going_lower():
    state, outcome = _plan(empty_state(), _inputs(floor=10.0))
    assert outcome.reason == "floor_below_reserve" and state.recovery is None


def test_no_episode_above_reserve():
    state, outcome = _plan(empty_state(), _inputs(soc=20.0))
    assert outcome.action == "none" and state.recovery is None


@pytest.mark.parametrize(("profile", "blocker"), [
    (None, "profile_missing"),
    (_profile(generated=NOW - timedelta(hours=4)), "profile_not_current"),
    (_profile(status="degraded"), "profile_quality_degraded"),
    (_profile(season="unknown"), "profile_season_unknown"),
    (_profile(season="extrapolated"), "profile_season_extrapolated"),
    (_profile(epoch=2), "profile_topology_mismatch"),
    (_profile(hours=24), "profile_horizon_short"),
])
def test_unusable_profile_never_authorizes_waiting(profile, blocker):
    state, outcome = _plan(empty_state(), _inputs(profile=profile))
    assert outcome.action == "grant_prepared"
    assert outcome.pv_blocker == blocker
    assert state.recovery.state == "grid_recovery"
    assert state.active_grant.identity.episode_id == state.recovery.episode_id


def test_missing_pv_forecast_never_authorizes_waiting():
    _, outcome = _plan(empty_state(), _inputs(pv=None))
    assert outcome.action == "grant_prepared" and outcome.pv_blocker == "pv_forecast_unusable"


def test_cloudy_forecast_creates_grant_to_target_only():
    state, outcome = _plan(empty_state(), _inputs(pv=lambda _t: 0.0))
    assert outcome.action == "grant_prepared" and outcome.pv_blocker is None
    identity = state.active_grant.identity
    assert identity.purpose == "recovery" and identity.target_pct == 25
    assert identity.budget_dc_kwh == pytest.approx(46.08 * (25 - 13) / 100)
    assert identity.checkpoint_dc_kwh == 7204.0
    # window = the off-peak steps needed for the budget at 1 kW DC (23 steps)
    assert state.active_grant.window.end == _nt_steps()[22] + PROFILE_STEP
    assert parse_store_state(json.loads(json.dumps(state.to_dict()))) == state


def test_house_load_is_subtracted_once():
    # need: (20 - 13) % of 46.08 kWh + 2 kWh margin = 5.23 kWh DC. Sun 6 h at 6 kW:
    # 6 - 5.05 = 0.95 kW x 6 h x 0.95 = 5.42 kWh DC -> enough (subtracted once);
    # subtracting the house twice would leave nothing and force a grid grant.
    state, outcome = _plan(empty_state(), _inputs(profile=_profile(kw=5.05)))
    assert outcome.action == "entered" and state.recovery.state == "wait_pv"
    _, outcome = _plan(empty_state(), _inputs(profile=_profile(kw=5.2)))
    assert outcome.action == "grant_prepared"  # 0.8 kW x 6 h x 0.95 = 4.56 < 5.23


def test_no_offpeak_window_prepares_no_grant():
    state, outcome = _plan(empty_state(), _inputs(pv=lambda _t: 0.0, steps=()))
    assert outcome.action == "no_window"
    assert state.active_grant is None and state.recovery.reason_code == "no_offpeak_window"


def test_counter_unavailable_anchors_but_creates_no_grant():
    state, outcome = _plan(empty_state(), _inputs(pv=lambda _t: 0.0, available=False))
    assert outcome.reason == "counter_unavailable"
    assert state.recovery is not None and state.active_grant is None


def test_not_ready_does_nothing():
    from custom_components.hallm_energy.lifecycle import set_reconcile

    state = set_reconcile(empty_state(), "required", "startup")
    assert _plan(state, _inputs())[0] == state


# --- anchored deadline (T44) -----------------------------------------------------------


def test_deadline_stays_anchored_across_recalculations_then_grid():
    state, _ = _plan(empty_state(), _inputs())
    deadline = state.recovery.wait_until
    episode = state.recovery.episode_id
    for i in range(1, 11):
        now = NOW + timedelta(minutes=15 * i)
        state, outcome = _plan(state, _inputs(now=now))
        assert outcome.action == "wait_pv"
        assert state.recovery.wait_until == deadline
        assert state.recovery.episode_id == episode
    later = NOW + timedelta(hours=3)
    state, outcome = _plan(state, _inputs(now=later, pv=lambda _t: 0.0))  # forecast dropped
    assert outcome.action == "grant_prepared"
    assert state.recovery.wait_until == deadline and state.recovery.episode_id == episode


def test_after_deadline_grid_recovery_without_moving_it():
    state, _ = _plan(empty_state(), _inputs())
    deadline = state.recovery.wait_until
    after = deadline + timedelta(minutes=5)
    state, outcome = _plan(state, _inputs(now=after, profile=_profile(now=after)))
    assert outcome.action == "grant_prepared"
    assert state.recovery.wait_until == deadline


def test_reserve_restored_by_pv_ends_episode_without_grant():
    state, _ = _plan(empty_state(), _inputs())
    state, outcome = _plan(state, _inputs(now=NOW + timedelta(hours=20), soc=21.0))
    assert outcome.action == "ended" and state.recovery is None and state.active_grant is None


# --- with a grant ----------------------------------------------------------------------


def _granted():
    state, _ = _plan(empty_state(), _inputs(pv=lambda _t: 0.0))
    return state


def test_grant_is_not_recreated_while_active():
    state = _granted()
    again, outcome = _plan(state, _inputs(now=NOW + timedelta(minutes=15), pv=lambda _t: 0.0))
    assert again == state and outcome.action == "none"


def test_expired_window_is_extended_same_id_and_budget():
    state = _granted()
    grant = state.active_grant
    later = grant.window.end + timedelta(minutes=1)
    steps = tuple(later + timedelta(hours=1) + i * PROFILE_STEP for i in range(40))
    extended, outcome = _plan(state, _inputs(now=later, steps=steps, pv=lambda _t: 0.0))
    assert outcome.action == "window_extended"
    new = extended.active_grant
    assert new.identity == grant.identity
    assert new.window.end > grant.window.end
    assert extended.pending_audit[-1].payload["kind"] == "grant_window_extended"


FLOORS = (10.0, 10.0, 10.0, 10.0, 10.0, 20.0)
RAISED = (25.0,) * 6
TOMORROW = (datetime(2026, 10, 28, 23, 0, tzinfo=UTC), datetime(2026, 10, 29, 23, 0, tzinfo=UTC))


def _closed(floors=FLOORS):
    """Grant prepared with the given floors, charged to target and closed."""
    state, _ = _plan(empty_state(), replace(_inputs(pv=lambda _t: 0.0), floors=floors))
    t = NOW + timedelta(hours=3)
    result = step_grant(state, now=t, soc_now_pct=25.0,
                        reading=CounterReading(COUNTER, 7209.0, t), reading_epoch="e1",
                        telemetry_valid=True, is_nt=lambda _t: True, deye_prepared=True,
                        safety_revision=1)
    return state, confirm_closed(result.state, now=t)


def _after(now, *, pv=_sunny, profile="default", floors=RAISED, soc=25.0):
    profile = _profile(now=now, hours=60) if profile == "default" else profile
    return replace(_inputs(now=now, soc=soc, pv=pv, profile=profile), floors=floors,
                   tomorrow=TOMORROW)


def test_grant_creation_saves_the_floors():
    state, _ = _plan(empty_state(), replace(_inputs(pv=lambda _t: 0.0), floors=FLOORS))
    assert state.recovery.floors_before == FLOORS


def test_closing_the_grant_holds_floors_until_decided():
    prepared, closed = _closed()
    assert closed.active_grant is None
    assert closed.recovery.state == "cloudy_hold"
    assert closed.recovery.episode_id == prepared.recovery.episode_id
    assert parse_store_state(json.loads(json.dumps(closed.to_dict()))) == closed


def test_cloudy_tomorrow_keeps_holding_at_target():
    _, closed = _closed()
    t = NOW + timedelta(hours=4)
    held, outcome = _plan(closed, _after(t, pv=lambda _t: 0.0))
    assert outcome.action == "hold" and outcome.reason == "cloudy"
    assert held.recovery.state == "cloudy_hold"


@pytest.mark.parametrize(("pv", "profile", "reason"), [
    (_sunny, None, "profile_missing"),
    (None, "default", "pv_forecast_unusable"),
    (_sunny, _profile(status="degraded", hours=60), "profile_quality_degraded"),
])
def test_unjudgeable_tomorrow_keeps_holding(pv, profile, reason):
    _, closed = _closed()
    t = NOW + timedelta(hours=4)
    if profile is not None and profile != "default":
        profile = _profile(now=t, status="degraded", hours=60)
    held, outcome = _plan(closed, _after(t, pv=pv, profile=profile))
    assert outcome.action == "hold" and outcome.reason == reason


def test_sunny_tomorrow_restores_then_ends_when_floors_read_back():
    prepared, closed = _closed()
    t = NOW + timedelta(hours=4)
    restoring, outcome = _plan(closed, _after(t))
    assert outcome.action == "restoring" and restoring.recovery.state == "restoring"
    still, outcome = _plan(restoring, _after(t + timedelta(minutes=1)))  # floors not back yet
    assert outcome.action == "restoring" and still == restoring
    done, outcome = _plan(restoring, _after(t + timedelta(minutes=2), floors=FLOORS))
    assert outcome.action == "ended" and done.recovery is None
    # a later drop is a new episode with a new id
    new_state, outcome = _plan(done, _inputs(now=t + timedelta(days=1), pv=lambda _t: 0.0))
    assert outcome.action == "grant_prepared"
    assert new_state.recovery.episode_id != prepared.recovery.episode_id


def test_unknown_saved_floors_hold_for_good():
    _, closed = _closed(floors=(10.0, None, 10.0, 10.0, 10.0, 20.0))
    assert closed.recovery.floors_before is None
    held, outcome = _plan(closed, _after(NOW + timedelta(hours=4)))
    assert outcome.action == "hold" and outcome.reason == "floors_unknown"


def test_no_new_episode_while_holding():
    _, closed = _closed()
    held, outcome = _plan(closed, _after(NOW + timedelta(hours=4), pv=lambda _t: 0.0, soc=15.0))
    assert outcome.action == "hold" and held.active_grant is None


def test_restart_keeps_episode_and_budget():
    state = _granted()
    restored = parse_store_state(json.loads(json.dumps(state.to_dict())))
    again, outcome = _plan(restored, _inputs(now=NOW + timedelta(minutes=30),
                                             pv=lambda _t: 0.0, soc=11.0))
    assert outcome.action == "none"
    assert again.active_grant.identity == state.active_grant.identity  # no new budget


def test_closed_episode_cannot_be_reprepared():
    from custom_components.hallm_energy.energy_core.grant import create_recovery_grant
    from custom_components.hallm_energy.lifecycle import GrantTransitionError

    state = _granted()
    episode = state.recovery.episode_id
    t = NOW + timedelta(hours=3)
    result = step_grant(state, now=t, soc_now_pct=25.0,
                        reading=CounterReading(COUNTER, 7209.0, t), reading_epoch="e1",
                        telemetry_valid=True, is_nt=lambda _t: True, deye_prepared=True,
                        safety_revision=1)
    closed = confirm_closed(result.state, now=t)
    grant = create_recovery_grant(grant_id="fresh", now=t, soc_now_pct=13, reserve_pct=20,
                                  recovery_target_pct=25, c_use_kwh=46.08, counter_id=COUNTER,
                                  checkpoint_dc_kwh=7209.0, window_end=t + timedelta(hours=2))
    with pytest.raises(GrantTransitionError, match="replay"):
        prepare_grant(closed, grant=grant, episode_id=episode, counter_epoch="e1", now=t,
                      policy_version="p")


# --- profile receiver ------------------------------------------------------------------


def test_receiver_keeps_newest_valid_profile():
    receiver = ProfileReceiver()
    good = _profile()
    receiver.handle(json.dumps(good.to_dict()), NOW)
    assert receiver.profile == good and receiver.error is None
    receiver.handle("{broken", NOW)
    assert receiver.profile == good and receiver.error.startswith("invalid")
    older = _profile(generated=NOW - timedelta(hours=1))
    receiver.handle(json.dumps(older.to_dict()), NOW)
    assert receiver.profile == good and receiver.error == "older_profile_rejected"
    bad = good.to_dict()
    bad["steps"].pop()
    receiver.handle(json.dumps(bad), NOW)
    assert receiver.profile == good
