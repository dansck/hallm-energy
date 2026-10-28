"""Morning exception over synthetic profile + forecast: window, gate, feasibility,
phase transitions and the rule that SOC below the reserve inside it is not a recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hallm_energy.energy_core.morning import MorningState
from custom_components.hallm_energy.morning_flow import (
    MorningInputs,
    MorningParams,
    plan_morning,
    pv_window,
)
from tests.test_lifecycle import empty_state
from tests.test_recovery_flow import _forecast, _inputs, _plan, _profile, _sunny

TZ = ZoneInfo("Europe/Prague")
DAY = datetime(2026, 10, 28, tzinfo=UTC)


def at(hour, minute=0, day=28):  # UTC
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


PARAMS = MorningParams(reserve_pct=20, dip_floor_pct=10, dip_h=3, refill_margin_min=30,
                       c_use_kwh=46.08, eta_c=0.95, eta_d=0.95, e_err_morning_kwh=2.0,
                       topology_epoch=1, enable_morning_dip=True)


def _in(now, *, soc=18.0, house_kw=1.0, pv=_sunny, grid=True, recovery=False, profile="d",
        forecast="d"):
    return MorningInputs(
        now=now, tz=TZ, soc_pct=soc,
        profile=_profile(now=now, kw=house_kw, hours=60) if profile == "d" else profile,
        pv=_forecast(pv, now=now) if forecast == "d" else forecast,
        grid_present=grid, in_recovery=recovery,
    )


def _plan_m(now, state=None, params=PARAMS, **kw):
    return plan_morning(state or empty_state(), _in(now, **kw), params)


def test_window_is_the_span_where_pv_beats_the_house_minus_the_margin():
    inputs = _in(at(5))
    start, end = pv_window(inputs.profile, inputs.pv, at(5), TZ, 30, phase_active=False)
    assert start == at(8) and end == at(13, 30)  # 09:00-14:30 CET


def test_window_moves_to_tomorrow_once_today_is_over():
    inputs = _in(at(15))
    start, _ = pv_window(inputs.profile, inputs.pv, at(15), TZ, 30, phase_active=False)
    assert start == at(8, day=29)


def test_no_window_without_a_surplus():
    inputs = _in(at(5), pv=lambda _t: 0.5)
    assert pv_window(inputs.profile, inputs.pv, at(5), TZ, 30, phase_active=False) is None


# --- entering the dip -----------------------------------------------------------------------


def test_dip_starts_inside_the_window_when_the_refill_is_feasible():
    state, out = _plan_m(at(6, 30))  # 07:30 CET, window 06:00-09:00
    assert out.phase == "morning_dip" and out.floor_pct == 10
    assert state.morning == MorningState("morning_dip")
    assert out.dip_window == (at(5), at(8))
    assert out.t_pv_start == at(8) and out.t_pv_end == at(13, 30)


def test_the_activation_gate_keeps_it_off():
    state, out = _plan_m(at(6, 30), params=replace(PARAMS, enable_morning_dip=False))
    assert out.phase == "none" and state.morning.phase == "none"


@pytest.mark.parametrize("kwargs", [
    {"now": at(4)},                      # before the window (05:00 CET)
    {"now": at(6, 30), "grid": False},   # island or unknown grid
    {"now": at(6, 30), "recovery": True},
    {"now": at(6, 30), "soc": 11.0},     # projected below the dip floor at sunrise
    {"now": at(6, 30), "house_kw": 5.9},  # PV barely beats the house: refill impossible
])
def test_dip_is_refused(kwargs):
    state, out = _plan_m(**kwargs)
    assert out.phase == "none" and state.morning.phase == "none"


@pytest.mark.parametrize("kwargs", [
    {"profile": None}, {"forecast": None},
    {"forecast": _forecast(_sunny, now=at(6, 30), retrieved=at(6, 30) - timedelta(hours=7))},
    {"forecast": _forecast(_sunny, now=at(6, 30), quality="degraded")},
    {"forecast": _forecast(_sunny, now=at(6, 30), hours=6)},   # does not cover the window
])
def test_unusable_inputs_never_start_a_dip(kwargs):
    state, out = _plan_m(at(6, 30), **kwargs)
    assert state.morning.phase == "none"


def test_unusable_inputs_end_an_active_dip():
    dip = empty_state(morning=MorningState("morning_dip"))
    state, out = _plan_m(at(7), state=dip, profile=None)
    assert state.morning.phase == "none" and out.reason == "profile_or_forecast_missing"
    state, out = _plan_m(at(7), state=dip, soc=None)
    assert state.morning.phase == "none" and out.reason == "telemetry_invalid"


# --- transitions ----------------------------------------------------------------------------


def test_dip_continues_while_feasible_then_becomes_pv_refill_at_sunrise():
    dip = empty_state(morning=MorningState("morning_dip"))
    state, out = _plan_m(at(7), state=dip, soc=16.0)
    assert out.phase == "morning_dip" and state == dip
    state, out = _plan_m(at(8, 5), state=dip, soc=14.0)  # past t_pv_start
    assert out.phase == "pv_refill" and out.floor_pct == 15
    assert state.morning == MorningState("pv_refill", 15.0)


def test_refill_floor_only_rises_and_is_capped_by_the_reserve():
    refill = empty_state(morning=MorningState("pv_refill", 15.0))
    state, out = _plan_m(at(9), state=refill, soc=14.0)  # SOC fell back: floor never lowered
    assert out.floor_pct == 15 and state.morning.floor_state_pct == 15.0
    state, out = _plan_m(at(9), state=refill, soc=17.0)  # ceil5(17) = 20 = the reserve
    assert out.floor_pct == 20 and state.morning.floor_state_pct == 20.0
    state, out = _plan_m(at(9), state=refill, soc=19.0)
    assert out.floor_pct == 20


def test_refill_ends_when_the_reserve_is_restored():
    refill = empty_state(morning=MorningState("pv_refill", 15.0))
    state, out = _plan_m(at(10), state=refill, soc=20.5)
    assert out.phase == "none" and state.morning.phase == "none" and not out.handoff


def test_refill_ends_at_t_pv_end_and_hands_over_to_recovery():
    refill = empty_state(morning=MorningState("pv_refill", 15.0))
    state, out = _plan_m(at(13, 40), state=refill, soc=17.0)  # after 13:30 UTC
    assert out.phase == "none" and out.handoff is True
    assert state.morning.phase == "none"


def test_worse_forecast_ends_the_refill_with_a_handoff():
    refill = empty_state(morning=MorningState("pv_refill", 15.0))
    state, out = _plan_m(at(10), state=refill, soc=15.0, pv=lambda _t: 1.2)  # barely any surplus
    assert out.phase == "none"


def test_with_a_grant_or_episode_no_phase_can_run():
    from tests.test_lifecycle import prepared

    state, out = plan_morning(prepared(), _in(at(6, 30)), PARAMS)
    assert state.morning.phase == "none" and out.reason == "recovery_in_progress"


# --- SOC below the reserve inside the exception is not a recovery trigger -------------------


@pytest.mark.parametrize("morning", [MorningState("morning_dip"), MorningState("pv_refill", 15.0)])
def test_soc_below_reserve_inside_a_phase_does_not_start_recovery(morning):
    state = empty_state(morning=morning)
    new_state, outcome = _plan(state, _inputs(soc=13.0, pv=lambda _t: 0.0))
    assert outcome.action == "none" and outcome.reason == f"morning_{morning.phase}"
    assert new_state.recovery is None and new_state.active_grant is None
