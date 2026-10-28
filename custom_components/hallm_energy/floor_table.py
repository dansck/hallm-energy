"""The floor (discharge floor = `capacity`) of every time-of-use slot — a pure
function, no I/O.

Each slot is classified against the TARIFF SCHEDULE, not its index (D-30):

* `vt` — wholly high tariff                       → `floor_vt` (reserve + comfort)
* `nt_day` — off-peak with more high tariff today → `floor_hold`
* `nt_night` — off-peak after the last high tariff of the day
                                                   → the night floor = the reserve
* `mixed` — high and off-peak parts (e.g. 10:00–16:00 with a 13–14 block):
  `floor_vt` while the tariff is high at the evaluation time, else `floor_hold`
* `unknown` — schedule unusable or not covering the slot → `floor_vt`
  (conservative: an unknown schedule means no grid charging and floors = `floor_vt`)

Overlays, in this order of precedence:

1. grant prepared / active / paused, or `cloudy_hold` after it: every floor is
   at least the grant target; the slot that is charging exactly the target;
2. `morning_dip`: high-tariff slots overlapping the window get the dip
   floor — the ONLY way a floor goes below the reserve besides
3. `pv_refill`: high-tariff / unknown slots get the state floor
   (`min(reserve, ceil5(max SOC since entry))`).

Every computed floor is rounded UP to the register's 5 % step (never down —
down would allow more discharge); the dip floor is the user's value as is.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal

SlotKind = Literal["vt", "nt_day", "nt_night", "mixed", "unknown"]
Phase = Literal["none", "morning_dip", "pv_refill"]
STEP = timedelta(minutes=15)
REGISTER_STEP_PCT = 5.0


def ceil5(value: float) -> float:
    return math.ceil(value / REGISTER_STEP_PCT - 1e-9) * REGISTER_STEP_PCT


@dataclass(frozen=True)
class FloorParams:
    reserve_pct: float
    comfort_pct: float
    dip_floor_pct: float

    @property
    def floor_vt_pct(self) -> float:
        return ceil5(self.reserve_pct + self.comfort_pct)


@dataclass(frozen=True)
class FloorContext:
    now: datetime  # aware
    tz: tzinfo
    slot_starts: tuple[time, ...]
    is_nt_at: Callable[[datetime], bool | None]  # tariff schedule; None = not known
    hold_floor_pct: float | None  # `floor_hold`; None = not computable → `floor_vt`
    grant_target_pct: float | None = None  # grant in flight or `cloudy_hold` target
    charging_slot: int | None = None  # slot a grant charges in right now
    phase: Phase = "none"
    refill_floor_pct: float | None = None  # `pv_refill` state floor
    dip_window: tuple[datetime, datetime] | None = None  # [start, end) of `morning_dip`


@dataclass(frozen=True)
class FloorPlan:
    floors: tuple[float, ...]
    kinds: tuple[SlotKind, ...]
    reasons: tuple[str, ...] = field(default_factory=tuple)
    reserve_pct: float = 0.0
    # slots whose floor is below the reserve BY the morning exception / refill state
    below_reserve_slots: tuple[int, ...] = ()

    def sanity_error(self) -> str | None:
        """A plan that must never be written: a floor below the reserve that no
        overlay explains, or one outside 0..100."""
        for i, floor in enumerate(self.floors):
            if not 0 <= floor <= 100:
                return f"slot{i + 1}:out_of_range"
            if floor < self.reserve_pct and i not in self.below_reserve_slots:
                return f"slot{i + 1}:below_reserve_without_exception"
        return None


def slot_intervals(starts: Sequence[time], now: datetime, tz: tzinfo
                   ) -> list[tuple[datetime, datetime]]:
    """The occurrence of every slot around `now`: slot i = [start_i, start_{i+1}),
    the last one ends at the first start of the next day; before the first start
    the last slot (which began yesterday) is the active one."""
    local = now.astimezone(tz)
    day = local.date()

    def at(d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=tz)

    n = len(starts)
    last_began_yesterday = local < at(day, starts[0])
    intervals = []
    for i in range(n):
        if i < n - 1:
            intervals.append((at(day, starts[i]), at(day, starts[i + 1])))
        elif last_began_yesterday:
            intervals.append((at(day - timedelta(days=1), starts[i]), at(day, starts[0])))
        else:
            intervals.append((at(day, starts[i]), at(day + timedelta(days=1), starts[0])))
    return intervals


def _tariffs(interval: tuple[datetime, datetime], is_nt_at: Callable[[datetime], bool | None]
             ) -> list[bool | None]:
    start, end = interval
    out, t = [], start
    while t < end:
        out.append(is_nt_at(t))
        t += STEP
    return out


def _local_day_end(t: datetime, tz: tzinfo) -> datetime:
    local = t.astimezone(tz)
    return datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=tz)


def classify_slot(interval: tuple[datetime, datetime], now: datetime, tz: tzinfo,
                  is_nt_at: Callable[[datetime], bool | None]) -> SlotKind:
    tariffs = _tariffs(interval, is_nt_at)
    if not tariffs or None in tariffs:
        return "unknown"
    if not any(tariffs):
        return "vt"
    if all(tariffs):
        # night = no high tariff left later the same local day
        rest = _tariffs((interval[0], _local_day_end(interval[0], tz)), is_nt_at)
        return "nt_night" if rest and all(t is True for t in rest) else "nt_day"
    return "mixed"


def floor_plan(params: FloorParams, ctx: FloorContext) -> FloorPlan:
    intervals = slot_intervals(ctx.slot_starts, ctx.now, ctx.tz)
    floor_vt = params.floor_vt_pct
    hold = floor_vt if ctx.hold_floor_pct is None else max(floor_vt, ceil5(ctx.hold_floor_pct))
    night = ceil5(params.reserve_pct)
    kinds: list[SlotKind] = []
    floors: list[float] = []
    reasons: list[str] = []
    below: list[int] = []
    for i, interval in enumerate(intervals):
        kind = classify_slot(interval, ctx.now, ctx.tz, ctx.is_nt_at)
        kinds.append(kind)
        if kind == "mixed":
            t_eval = min(max(ctx.now, interval[0]), interval[1] - STEP)
            high_now = ctx.is_nt_at(t_eval) is False
            base = floor_vt if high_now else hold
        elif kind == "nt_day":
            base = hold
        elif kind == "nt_night":
            base = night
        else:  # vt, unknown
            base = floor_vt
        floor = base
        high = kind in ("vt", "unknown") or (kind == "mixed" and base == floor_vt)

        if ctx.phase == "morning_dip" and ctx.dip_window is not None and high \
                and kind != "unknown" \
                and interval[0] < ctx.dip_window[1] and ctx.dip_window[0] < interval[1]:
            floor = params.dip_floor_pct
            below.append(i)
            reasons.append(f"slot{i + 1}:morning_dip")
        elif ctx.phase == "pv_refill" and high and ctx.refill_floor_pct is not None:
            floor = min(params.reserve_pct, ceil5(ctx.refill_floor_pct))
            below.append(i)
            reasons.append(f"slot{i + 1}:pv_refill")

        if ctx.grant_target_pct is not None:
            target = ceil5(ctx.grant_target_pct)
            floor = target if i == ctx.charging_slot else max(floor, target)
            if floor >= params.reserve_pct and i in below:
                below.remove(i)
            reasons.append(f"slot{i + 1}:grant")
        floors.append(floor)
    if "unknown" in kinds:
        reasons.append("schedule_unknown_for_some_slots")
    return FloorPlan(tuple(floors), tuple(kinds), tuple(reasons),
                     reserve_pct=params.reserve_pct, below_reserve_slots=tuple(below))


def hold_floor_pct(
    *, now: datetime, tz: tzinfo, floor_vt_pct: float,
    is_nt_at: Callable[[datetime], bool | None],
    house_p90_kw: Callable[[datetime], float | None],
    pv_kw: Callable[[datetime], float | None],
    c_use_kwh: float, eta_d: float,
) -> float | None:
    """`floor_hold = floor_vt + ceil5(100 × E_vt_rest_P90 / (eta_d × C_use))` where
    `E_vt_rest_P90` is the house load (P90) of the high-tariff steps still to come
    today that the (corrected) PV forecast does not cover. `None` when a needed
    value is missing — never guessed."""
    end = _local_day_end(now, tz)
    t = now.replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 15)
    if t < now:
        t += STEP
    energy_kwh = 0.0
    while t < end:
        nt = is_nt_at(t)
        if nt is None:
            return None
        if not nt:
            house, pv = house_p90_kw(t), pv_kw(t)
            if house is None or pv is None:
                return None
            energy_kwh += max(0.0, house - pv) * STEP.total_seconds() / 3600
        t += STEP
    extra = ceil5(100 * energy_kwh / (eta_d * c_use_kwh)) if energy_kwh > 0 else 0.0
    return floor_vt_pct + extra
