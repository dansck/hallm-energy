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
from custom_components.hallm_energy.energy_core.pv_forecast import (
    PV_STEP,
    PvForecast,
    PvStep,
)
from custom_components.hallm_energy.lifecycle import confirm_closed, prepare_grant, step_grant
from custom_components.hallm_energy.profile_receiver import ProfileReceiver
from custom_components.hallm_energy.recovery_flow import (
    RecoveryInputs,
    RecoveryParams,
    floor_writes,
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


def _forecast(fn, *, now=NOW, hours=60, retrieved=None, quality="ok", epoch=1):
    """PV snapshot per contract from a kW-of-time function (correction already in)."""
    retrieved = retrieved or now - timedelta(minutes=10)
    start = retrieved.replace(minute=0, second=0, microsecond=0)
    steps = tuple(PvStep(start + i * PV_STEP, fn(start + i * PV_STEP),
                         fn(start + i * PV_STEP) / 0.75) for i in range(hours * 4))
    return PvForecast(
        snapshot_id="s", model_name="m", model_version="v", topology_epoch=epoch,
        retrieved_at=retrieved, source_issued_at=None,
        valid_until=retrieved + timedelta(hours=6), provider="test", weather_model=None,
        horizon_start=start, horizon_end=start + timedelta(hours=hours), branches=("a",),
        corrections={"pv_correction": 0.75}, quality=quality, notes=(), steps=steps)


def _inputs(*, now=NOW, soc=13.0, floor=20.0, pv=_sunny, profile="default", steps=None,
            raw=7204.0, available=True):
    forecast = pv if pv is None or isinstance(pv, PvForecast) else _forecast(pv, now=now)
    return RecoveryInputs(
        now=now, soc_pct=soc, current_floor_pct=floor,
        reading=CounterReading(COUNTER, raw, now, available=available), reading_epoch="e1",
        pv=forecast, profile=_profile(now=now) if profile == "default" else profile,
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


@pytest.mark.parametrize(("pv", "reason"), [
    (_forecast(_sunny, retrieved=NOW - timedelta(hours=7)), "pv_forecast_expired"),
    (_forecast(_sunny, quality="degraded"), "pv_forecast_quality_degraded"),
    (_forecast(_sunny, hours=24), "pv_forecast_horizon_short"),
    (_forecast(_sunny, epoch=2), "pv_forecast_topology_mismatch"),
])
def test_unusable_pv_forecast_never_authorizes_waiting(pv, reason):
    _, outcome = _plan(empty_state(), _inputs(pv=pv))
    assert outcome.action == "grant_prepared" and outcome.pv_blocker == reason


def test_missing_pv_forecast_never_authorizes_waiting():
    _, outcome = _plan(empty_state(), _inputs(pv=None))
    assert outcome.action == "grant_prepared" and outcome.pv_blocker == "pv_forecast_missing"


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


RESTORE = (20.0,) * 6  # found floors, never below the reserve


def _after(now, *, pv=_sunny, profile="default", floors=RAISED, soc=25.0, live=True):
    profile = _profile(now=now, hours=60) if profile == "default" else profile
    return replace(_inputs(now=now, soc=soc, pv=pv, profile=profile), floors=floors,
                   outlook=(max(TOMORROW[0], now), TOMORROW[1]), live=live)


def test_grant_creation_saves_restore_floors_never_below_reserve():
    state, _ = _plan(empty_state(), replace(_inputs(pv=lambda _t: 0.0), floors=FLOORS))
    assert state.recovery.restore_floors == RESTORE  # 10 % found -> 20 % (reserve)
    assert state.recovery.hold_floor_pct == 25
    higher, _ = _plan(empty_state(), replace(_inputs(pv=lambda _t: 0.0),
                                             floors=(30.0,) * 5 + (20.0,)))
    assert higher.recovery.restore_floors == (30.0,) * 5 + (20.0,)


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
    (None, "default", "pv_forecast_missing"),
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
    done, outcome = _plan(restoring, _after(t + timedelta(minutes=2), floors=RESTORE))
    assert outcome.action == "ended" and done.recovery is None
    # a later drop is a new episode with a new id
    new_state, outcome = _plan(done, _inputs(now=t + timedelta(days=1), pv=lambda _t: 0.0))
    assert outcome.action == "grant_prepared"
    assert new_state.recovery.episode_id != prepared.recovery.episode_id


@pytest.mark.parametrize(("change", "reason"), [
    ({"pv": None}, "pv_forecast_missing"),
    ({"profile": None}, "profile_missing"),
    ({"pv": lambda _t: 0.0}, "cloudy"),
])
def test_restoring_is_revalidated_and_falls_back_to_hold(change, reason):
    _, closed = _closed()
    t = NOW + timedelta(hours=4)
    restoring, _ = _plan(closed, _after(t))
    partial = (20.0, 20.0, 25.0, 25.0, 25.0, 25.0)  # restore interrupted half way
    later = t + timedelta(hours=4)
    held, outcome = _plan(restoring, _after(later, floors=partial, **change))
    assert outcome.action == "hold" and outcome.reason == reason
    assert held.recovery.state == "cloudy_hold"
    # the already lowered floors go back up to the hold floor
    assert floor_writes(held.recovery, partial) == [(0, 25.0), (1, 25.0)]


def test_restart_does_not_continue_restoring_blindly():
    _, closed = _closed()
    t = NOW + timedelta(hours=4)
    restoring, _ = _plan(closed, _after(t))
    restored = parse_store_state(json.loads(json.dumps(restoring.to_dict())))
    held, outcome = _plan(restored, _after(t + timedelta(hours=1), profile=None))
    assert outcome.action == "hold"


def test_floor_changed_from_outside_is_a_conflict_not_overwritten():
    _, closed = _closed()
    t = NOW + timedelta(hours=4)
    manual = (25.0, 15.0, 25.0, 25.0, 25.0, 25.0)
    held, outcome = _plan(closed, _after(t, floors=manual))
    assert outcome.action == "hold" and outcome.reason == "floor_conflict"
    assert floor_writes(held.recovery, manual) is None


def test_shadow_reports_restore_once_then_ends():
    _, closed = _closed()
    t = NOW + timedelta(hours=4)
    shadow_floors = FLOORS  # never raised in shadow: not a conflict there
    restoring, outcome = _plan(closed, _after(t, floors=shadow_floors, live=False))
    assert outcome.action == "restoring"
    done, outcome = _plan(restoring, _after(t + timedelta(seconds=30), floors=shadow_floors,
                                            live=False))
    assert outcome.action == "ended" and done.recovery is None


def test_unknown_saved_floors_hold_for_good():
    _, closed = _closed(floors=(10.0, None, 10.0, 10.0, 10.0, 20.0))
    assert closed.recovery.restore_floors is None
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


def test_pv_receiver_keeps_newest_valid_forecast_and_never_refreshes_it():
    from custom_components.hallm_energy.profile_receiver import PvForecastReceiver

    receiver = PvForecastReceiver()
    good = _forecast(_sunny)
    receiver.handle(json.dumps(good.to_dict()), NOW)
    assert receiver.forecast == good and receiver.error is None
    receiver.handle("{broken", NOW)
    assert receiver.forecast == good and receiver.error.startswith("invalid")
    older = _forecast(_sunny, retrieved=NOW - timedelta(hours=2))
    receiver.handle(json.dumps(older.to_dict()), NOW)
    assert receiver.forecast == good and receiver.error == "older_forecast_rejected"
    # a replay of the same retained message keeps its original retrieval time
    receiver.handle(json.dumps(good.to_dict()), NOW + timedelta(hours=5))
    assert receiver.forecast.retrieved_at == good.retrieved_at


# --- managed floors: the table, not saved floors, is the authority -----------------------


def test_managed_after_grant_needs_no_saved_floors_and_ends_when_the_plan_is_read_back():
    prepared_state, closed = _closed(floors=(10.0, None, 10.0, 10.0, 10.0, 20.0))
    assert closed.recovery.restore_floors is None  # unknown at grant time
    t = NOW + timedelta(hours=4)
    plan = (25.0, 25.0, 25.0, 25.0, 25.0, 20.0)
    restoring, outcome = _plan(closed, replace(_after(t), managed_floors=plan))
    assert outcome.action == "restoring"  # not "floors_unknown"
    # the plan was for the state before this transition: not done yet
    still, outcome = _plan(restoring, replace(_after(t + timedelta(minutes=1), floors=RAISED),
                                              managed_floors=plan))
    assert outcome.action == "restoring"
    done, outcome = _plan(restoring, replace(_after(t + timedelta(minutes=2), floors=plan),
                                             managed_floors=plan))
    assert outcome.action == "ended" and done.recovery is None


def test_managed_hold_still_follows_the_forecast():
    _, closed = _closed()
    t = NOW + timedelta(hours=4)
    held, outcome = _plan(closed, replace(_after(t, pv=lambda _t: 0.0),
                                          managed_floors=(25.0,) * 6))
    assert outcome.action == "hold" and outcome.reason == "cloudy"


def test_managed_ignores_floors_changed_from_outside_because_the_plan_rewrites_them():
    _, closed = _closed()
    t = NOW + timedelta(hours=4)
    manual = (25.0, 15.0, 25.0, 25.0, 25.0, 25.0)
    _, outcome = _plan(closed, replace(_after(t, floors=manual), managed_floors=(25.0,) * 6))
    assert outcome.action == "restoring"  # no floor_conflict: the table is the authority


def test_managed_entry_does_not_depend_on_the_found_floor():
    inputs = replace(_inputs(pv=lambda _t: 0.0, floor=10.0), managed_floors=(25.0,) * 6)
    _, outcome = _plan(empty_state(), inputs)
    assert outcome.action == "grant_prepared"
    _, outcome = _plan(empty_state(), _inputs(pv=lambda _t: 0.0, floor=10.0))
    assert outcome.reason == "floor_below_reserve"


# --- a prepared grant is re-judged until charging was ever enabled -------------------------


def _born_without_data(floors=FLOORS):
    """The situation of 28. 10. 17:45: below the reserve, no profile and no forecast yet."""
    state, outcome = _plan(empty_state(), replace(_inputs(profile=None, pv=None), floors=floors))
    assert outcome.action == "grant_prepared" and outcome.pv_blocker == "profile_missing"
    return state


def test_a_grant_born_without_data_is_cancelled_when_the_pv_data_arrive():
    prepared = _born_without_data()
    grant, episode = prepared.active_grant, prepared.recovery
    later = NOW + timedelta(minutes=10)
    state, outcome = _plan(prepared, _inputs(now=later))  # profile + forecast now there
    assert outcome.action == "grant_cancelled" and outcome.reason == "pv_restores_reserve"
    assert state.active_grant is None
    # the episode is closed for good; the recovery continues under a NEW id, same deadline
    (entry,) = state.episode_ledger
    assert entry.state == "closed" and entry.close_reason == "pv_restores_reserve"
    assert entry.episode_id == episode.episode_id
    assert state.recovery.state == "wait_pv" and state.recovery.episode_id != episode.episode_id
    assert state.recovery.wait_until == episode.wait_until  # anchored, never moved
    assert state.recovery.restore_floors == RESTORE and state.recovery.hold_floor_pct == 25
    event = state.pending_audit[-1]
    assert event.event_id == f"{grant.identity.grant_id}:cancelled"
    assert event.payload["kind"] == "grant_cancelled"
    assert parse_store_state(json.loads(json.dumps(state.to_dict()))) == state


def test_the_cancelled_episode_can_still_need_a_grid_grant_later_without_a_replay():
    prepared = _born_without_data()
    cancelled, _ = _plan(prepared, _inputs(now=NOW + timedelta(minutes=10)))
    deadline = cancelled.recovery.wait_until
    after = deadline + timedelta(minutes=5)
    state, outcome = _plan(cancelled, _inputs(now=after, profile=_profile(now=after, hours=60),
                                              pv=_forecast(_sunny, now=after)))
    assert outcome.action == "grant_prepared"  # the wait is over: the grid, a NEW episode
    assert state.active_grant.identity.episode_id == state.recovery.episode_id
    assert state.recovery.wait_until == deadline
    assert state.recovery.restore_floors == RESTORE  # still the floors found originally
    assert len(state.episode_ledger) == 2


@pytest.mark.parametrize("kwargs", [
    {"pv": lambda _t: 0.0},                       # cloudy: still needs the grid
    {"profile": None},                            # not judgeable: the conservative grant stays
    {"pv": _forecast(_sunny, now=NOW + timedelta(minutes=10), quality="degraded")},
    {"pv": _forecast(_sunny, now=NOW + timedelta(minutes=10), hours=24)},   # horizon too short
])
def test_a_prepared_grant_stays_when_the_data_do_not_support_waiting(kwargs):
    prepared = _born_without_data()
    state, outcome = _plan(prepared, _inputs(now=NOW + timedelta(minutes=10), **kwargs))
    assert outcome.action != "grant_cancelled" and state.active_grant is not None


def test_a_prepared_grant_is_cancelled_when_the_reserve_is_restored_and_the_episode_ends():
    prepared = _born_without_data()
    state, outcome = _plan(prepared, _inputs(now=NOW + timedelta(hours=3), soc=21.0))
    assert outcome.action == "grant_cancelled" and outcome.reason == "reserve_restored"
    assert state.active_grant is None and state.recovery is None


def test_after_the_wait_deadline_the_grid_grant_is_not_cancelled():
    prepared = _born_without_data()
    after = prepared.recovery.wait_until + timedelta(minutes=1)
    state, outcome = _plan(prepared, _inputs(now=after, profile=_profile(now=after, hours=60),
                                             pv=_forecast(_sunny, now=after)))
    assert state.active_grant is not None and outcome.action != "grant_cancelled"


@pytest.mark.parametrize("status", ["active", "paused", "closing"])
def test_a_grant_that_was_ever_enabled_is_never_cancelled(status):
    from custom_components.hallm_energy.state_schema import ActiveGrant

    prepared = _born_without_data()
    running = replace(prepared, active_grant=replace(prepared.active_grant, status=status))
    assert isinstance(running.active_grant, ActiveGrant)
    state, outcome = _plan(running, _inputs(now=NOW + timedelta(minutes=10)))
    assert state.active_grant is not None and outcome.action != "grant_cancelled"


def test_a_grant_with_a_pending_step_is_never_cancelled():
    from custom_components.hallm_energy.lifecycle import step_grant

    prepared = _born_without_data()
    t = NOW + timedelta(minutes=10)
    proposed = step_grant(prepared, now=t, soc_now_pct=13.0,
                          reading=CounterReading(COUNTER, 7204.0, t), reading_epoch="e1",
                          telemetry_valid=True, is_nt=lambda _t: True, deye_prepared=True,
                          safety_revision=1)
    assert proposed.state.pending_step is not None
    state, outcome = _plan(proposed.state, _inputs(now=t))
    assert state.active_grant is not None and outcome.action != "grant_cancelled"


# --- outlook ---------------------------------------------------------------------------------


def _outlook(pv, profile="default", now=NOW):
    from custom_components.hallm_energy.recovery_flow import outlook_status
    window = (max(TOMORROW[0], now), TOMORROW[1] + timedelta(days=1))
    prof = _profile(now=now, hours=72) if profile == "default" else profile
    forecast = None if pv is None else _forecast(pv, now=now, hours=72)
    return outlook_status(prof, forecast, now, window, 1)


def test_outlook_is_good_when_pv_covers_the_house_over_all_days():
    label, balance, why = _outlook(_sunny)
    assert label == "good" and balance > 0 and why is None


def test_outlook_is_poor_when_pv_falls_short_of_the_house():
    label, balance, _ = _outlook(lambda _t: 0.0)
    assert label == "poor" and balance < 0


@pytest.mark.parametrize(("pv", "profile"), [(None, "default"), (_sunny, None)])
def test_outlook_is_unknown_without_a_snapshot(pv, profile):
    label, balance, why = _outlook(pv, profile)
    assert label == "unknown" and balance is None and why


def test_outlook_is_unknown_when_a_snapshot_does_not_reach_the_window_end():
    from custom_components.hallm_energy.recovery_flow import outlook_status
    window = (TOMORROW[0], TOMORROW[1] + timedelta(days=2))
    label, _, why = outlook_status(_profile(hours=48), _forecast(_sunny, hours=48), NOW,
                                   window, 1)
    assert label == "unknown" and why
