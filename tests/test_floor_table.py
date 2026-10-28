"""Floor table: classification by tariff, overlays, hold floor."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from custom_components.hallm_energy.floor_table import (
    FloorContext,
    FloorParams,
    ceil5,
    classify_slot,
    floor_plan,
    hold_floor_pct,
    slot_intervals,
)

TZ = ZoneInfo("Europe/Prague")
STARTS = (time(6), time(7), time(9), time(10), time(16), time(17))
PARAMS = FloorParams(reserve_pct=20, outlook_margin_pct=5, dip_floor_pct=10)
VT_HOURS = {6, 9, 13, 16}  # D57d: 06-07, 09-10, 13-14, 16-17


def local(h, m=0, day=28):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def d57d(t: datetime) -> bool:
    return t.astimezone(TZ).hour not in VT_HOURS  # True = off-peak


def unknown(_t):
    return None


def ctx(now, *, is_nt_at=d57d, hold=None, **kw):
    return FloorContext(now=now.astimezone(UTC), tz=TZ, slot_starts=STARTS,
                        is_nt_at=is_nt_at, hold_floor_pct=hold, **kw)


def test_slots_are_classified_by_tariff_not_index():
    plan = floor_plan(PARAMS, ctx(local(8)))
    assert plan.kinds == ("vt", "nt_day", "vt", "mixed", "vt", "nt_night")


def test_base_floors_follow_the_table():
    plan = floor_plan(PARAMS, ctx(local(8), hold=40))
    # 06-07 VT, 07-09 NT (hold), 09-10 VT, 10-16 NT now (hold), 16-17 VT, night = reserve
    assert plan.floors == (20, 40, 20, 40, 20, 20)


def test_mixed_slot_follows_the_tariff_at_its_evaluation_time():
    at_vt = floor_plan(PARAMS, ctx(local(13, 30), hold=40))
    assert at_vt.floors[3] == 20  # VT right now
    at_nt = floor_plan(PARAMS, ctx(local(11), hold=40))
    assert at_nt.floors[3] == 40


def test_night_slot_before_the_first_start_began_yesterday():
    plan = floor_plan(PARAMS, ctx(local(3), hold=40))
    assert plan.kinds[5] == "nt_night" and plan.floors[5] == 20
    assert slot_intervals(STARTS, local(3), TZ)[5][0] == local(17, day=27)


def test_a_shifted_tariff_moves_the_floors_not_the_slot_index():
    shifted = lambda t: t.astimezone(TZ).hour not in {7, 8, 13, 16}  # noqa: E731
    plan = floor_plan(PARAMS, ctx(local(8), is_nt_at=shifted, hold=40))
    assert plan.kinds[0] == "nt_day" and plan.kinds[1] == "vt" and plan.kinds[2] == "nt_day"
    assert plan.floors[:3] == (40, 20, 40)


def test_unknown_schedule_means_floor_vt_everywhere():
    plan = floor_plan(PARAMS, ctx(local(8), is_nt_at=unknown, hold=40))
    assert set(plan.kinds) == {"unknown"} and set(plan.floors) == {20}
    assert "schedule_unknown_for_some_slots" in plan.reasons


def test_hold_is_never_below_floor_vt_and_rounded_up():
    assert floor_plan(PARAMS, ctx(local(8), hold=None)).floors[1] == 20
    assert floor_plan(PARAMS, ctx(local(8), hold=10)).floors[1] == 20
    assert floor_plan(PARAMS, ctx(local(8), hold=41)).floors[1] == 45


def test_without_overlays_nothing_is_below_the_reserve():
    for h in range(24):
        assert min(floor_plan(PARAMS, ctx(local(h), hold=None)).floors) >= 20


# --- morning exception ----------------------------------------------------------------


def test_morning_dip_lowers_only_the_high_tariff_slots_in_the_window():
    window = (local(6).astimezone(UTC), local(9).astimezone(UTC))
    plan = floor_plan(PARAMS, ctx(local(7, 30), hold=40, phase="morning_dip",
                                  dip_window=window))
    assert plan.floors == (10, 40, 20, 40, 20, 20)  # 09-10 starts at the window end
    window = (local(7).astimezone(UTC), local(10).astimezone(UTC))
    plan = floor_plan(PARAMS, ctx(local(7, 30), hold=40, phase="morning_dip",
                                  dip_window=window))
    assert plan.floors == (20, 40, 10, 40, 20, 20)


def test_morning_dip_never_applies_to_an_unknown_schedule():
    window = (local(6).astimezone(UTC), local(9).astimezone(UTC))
    plan = floor_plan(PARAMS, ctx(local(7), is_nt_at=unknown, phase="morning_dip",
                                  dip_window=window))
    assert set(plan.floors) == {20}


@pytest.mark.parametrize(("state", "expected"), [(15.0, 15.0), (22.0, 20.0), (10.0, 10.0)])
def test_pv_refill_state_floor_replaces_floor_vt_and_is_capped_by_the_reserve(state, expected):
    plan = floor_plan(PARAMS, ctx(local(10), hold=40, phase="pv_refill",
                                  refill_floor_pct=state))
    assert plan.floors[0] == expected and plan.floors[2] == expected
    assert plan.floors[1] == 40 and plan.floors[5] == 20  # off-peak slots keep their floors


# --- grant and hold after it -------------------------------------------------------------


def test_grant_raises_every_floor_and_the_charging_slot_is_exactly_the_target():
    plan = floor_plan(PARAMS, ctx(local(23), hold=None, grant_target_pct=25, charging_slot=5))
    assert plan.floors == (25, 25, 25, 25, 25, 25)
    high = floor_plan(PARAMS, ctx(local(8), hold=45, grant_target_pct=25, charging_slot=1))
    assert high.floors == (25, 25, 25, 45, 25, 25)  # charging slot exactly the target


def test_grant_overrides_a_morning_overlay():
    window = (local(6).astimezone(UTC), local(9).astimezone(UTC))
    plan = floor_plan(PARAMS, ctx(local(7), phase="morning_dip", dip_window=window,
                                  grant_target_pct=25))
    assert min(plan.floors) == 25


# --- floor_hold ---------------------------------------------------------------------------


def _hold(now, *, house=1.5, pv=0.0, is_nt_at=d57d):
    return hold_floor_pct(now=now.astimezone(UTC), tz=TZ, floor_vt_pct=25, is_nt_at=is_nt_at,
                          house_p90_kw=lambda _t: house, pv_kw=lambda _t: pv,
                          c_use_kwh=46.08, eta_d=0.95)


def test_hold_adds_the_energy_of_the_remaining_uncovered_vt_blocks():
    # from 08:00: VT 09-10, 13-14, 16-17 = 3 h x 1.5 kW = 4.5 kWh -> 10.3 % -> 15 %
    assert _hold(local(8)) == 25 + 15
    assert _hold(local(14, 30)) == 25 + 5  # only 16-17 left: 1.5 kWh -> 3.4 % -> 5 %
    assert _hold(local(20)) == 25  # no VT left today


def test_hold_counts_only_what_pv_does_not_cover():
    assert _hold(local(8), pv=2.0) == 25  # PV covers the house in every VT step
    assert _hold(local(8), pv=1.0) == 25 + 5  # 0.5 kW x 3 h = 1.5 kWh


@pytest.mark.parametrize("bad", ["schedule", "house", "pv"])
def test_hold_is_unknown_when_an_input_is_missing(bad):
    kwargs = {}
    if bad == "schedule":
        kwargs["is_nt_at"] = unknown
    got = hold_floor_pct(
        now=local(8).astimezone(UTC), tz=TZ, floor_vt_pct=25,
        is_nt_at=kwargs.get("is_nt_at", d57d),
        house_p90_kw=(lambda _t: None) if bad == "house" else (lambda _t: 1.0),
        pv_kw=(lambda _t: None) if bad == "pv" else (lambda _t: 0.0),
        c_use_kwh=46.08, eta_d=0.95)
    assert got is None


def test_classify_and_rounding_helpers():
    interval = (local(10), local(16))
    assert classify_slot(interval, local(8), TZ, d57d) == "mixed"
    assert ceil5(20.0) == 20 and ceil5(20.1) == 25 and ceil5(0) == 0


# --- outlook --------------------------------------------------------------------------


def test_poor_outlook_raises_every_floor_to_reserve_plus_margin():
    plan = floor_plan(PARAMS, ctx(local(8), hold=40, outlook_poor=True))
    assert plan.floors == (25, 40, 25, 40, 25, 25)
    assert "outlook_poor" in plan.reasons


def test_good_outlook_adds_nothing_to_the_reserve():
    plan = floor_plan(PARAMS, ctx(local(8), hold=None, outlook_poor=False))
    assert set(plan.floors) == {20} and "outlook_poor" not in plan.reasons


def test_morning_dip_still_lowers_below_a_poor_outlook_floor():
    window = (local(6).astimezone(UTC), local(9).astimezone(UTC))
    plan = floor_plan(PARAMS, ctx(local(7, 30), hold=40, phase="morning_dip",
                                  dip_window=window, outlook_poor=True))
    assert plan.floors[0] == 10 and plan.floors[2] == 25


def test_grant_target_wins_over_the_outlook_floor():
    plan = floor_plan(PARAMS, ctx(local(23), grant_target_pct=30, charging_slot=5,
                                  outlook_poor=True))
    assert plan.floors[5] == 30
