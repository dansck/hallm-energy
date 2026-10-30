"""Is there sun worth waiting for? A pure look at the coming days.

For every local day the surplus is the corrected PV production minus the expected house load; only
the stretches where that surplus reaches the wallbox minimum count, because below it the wallbox
cannot be fed from the sun alone. The first day with enough such energy is the first opportunity
to charge from the sun.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo

STEP = timedelta(minutes=15)


def first_sun_day(
    pv_kw: Callable[[datetime], float | None],
    house_kw: Callable[[datetime], float | None],
    *,
    start: datetime,
    days: int,
    tz: tzinfo,
    min_surplus_kw: float,
    min_kwh: float,
) -> tuple[int | None, float, bool]:
    """`(day, kwh, covered)`: the first local day (0 = today, counted from `start`) whose surplus
    energy reaches `min_kwh`, that energy, and whether the forecast and the profile covered every
    step of the span. `None` for the day when no day qualifies; an uncovered span only ever
    lowers the result, so a missing forecast never makes the sun look better."""
    start_local = start.astimezone(tz)
    today = start_local.date()
    end = (start_local.replace(hour=0, minute=0, second=0, microsecond=0)
           + timedelta(days=days + 1)).astimezone(start.tzinfo)
    per_day: dict[int, float] = {}
    covered = True
    t = start
    while t < end:
        pv, house = pv_kw(t), house_kw(t)
        if pv is None or house is None:
            covered = False
        else:
            surplus = pv - house
            if surplus >= min_surplus_kw:
                day = (t.astimezone(tz).date() - today).days
                per_day[day] = per_day.get(day, 0.0) + surplus * STEP.total_seconds() / 3600.0
        t += STEP
    for day in sorted(per_day):
        if per_day[day] >= min_kwh:
            return day, per_day[day], covered
    return None, 0.0, covered
