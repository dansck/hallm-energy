"""House load profile snapshot (schema v1): structure, strict parsing and the
receiver-side check whether a profile may authorize waiting for PV.

The profile is house AC consumption without the EV, in kW, as P10/P50/P90 per
contiguous 15-minute UTC step. A missing, stale, incomplete or low-quality
profile never authorizes waiting for PV; the caller falls back to its
conservative branch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

PROFILE_SCHEMA_VERSION = 1
PROFILE_STEP = timedelta(minutes=15)

QualityStatus = Literal["ok", "degraded", "invalid"]
SeasonCoverage = Literal["ok", "extrapolated", "unknown"]


class ProfileError(ValueError):
    """Malformed profile payload."""


@dataclass(frozen=True)
class ProfileStep:
    t: datetime
    p10: float
    p50: float
    p90: float


@dataclass(frozen=True)
class ProfileQuality:
    status: QualityStatus
    history_start: datetime
    history_end: datetime
    days_used: int
    min_samples_per_step: int
    history_quarters_expected: int
    history_quarters_used: int
    season_coverage: SeasonCoverage
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class HouseLoadProfile:
    profile_id: str
    model_name: str
    model_version: str
    topology_epoch: int
    generated_at: datetime
    valid_until: datetime
    horizon_start: datetime
    horizon_end: datetime
    quality: ProfileQuality
    steps: tuple[ProfileStep, ...]

    def p50_at(self, at: datetime) -> float | None:
        """P50 of the step containing `at`; `None` outside the horizon."""
        if not self.horizon_start <= at < self.horizon_end:
            return None
        index = int((at - self.horizon_start) / PROFILE_STEP)
        return self.steps[index].p50

    def to_dict(self) -> dict[str, Any]:
        q = self.quality
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "model": {"name": self.model_name, "version": self.model_version},
            "topology_epoch": self.topology_epoch,
            "generated_at": _iso(self.generated_at),
            "valid_until": _iso(self.valid_until),
            "horizon": {
                "start": _iso(self.horizon_start),
                "end": _iso(self.horizon_end),
                "step_minutes": 15,
            },
            "unit": "kW",
            "boundary": {
                "measure": "house_ac_without_ev",
                "included": ["heating", "air_conditioning"],
                "excluded": ["ev_wallbox"],
            },
            "quality": {
                "status": q.status,
                "history_start": _iso(q.history_start),
                "history_end": _iso(q.history_end),
                "days_used": q.days_used,
                "min_samples_per_step": q.min_samples_per_step,
                "history_quarters_expected": q.history_quarters_expected,
                "history_quarters_used": q.history_quarters_used,
                "season_coverage": q.season_coverage,
                "notes": list(q.notes),
            },
            "steps": [
                {"t": _iso(s.t), "p10": s.p10, "p50": s.p50, "p90": s.p90} for s in self.steps
            ],
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _time(raw: Any, where: str) -> datetime:
    if not isinstance(raw, str):
        raise ProfileError(f"{where}: expected an ISO time string")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ProfileError(f"{where}: invalid time {raw!r}") from None
    if value.tzinfo is None:
        raise ProfileError(f"{where}: time without offset")
    return value.astimezone(UTC)


def _dict(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: expected an object")
    return raw


def _str(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ProfileError(f"{where}: expected a non-empty string")
    return raw


def _int(raw: Any, where: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProfileError(f"{where}: expected an integer")
    return raw


def _kw(raw: Any, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ProfileError(f"{where}: expected a number")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ProfileError(f"{where}: expected a finite value >= 0")
    return value


def parse_profile(raw: Any) -> HouseLoadProfile:
    """Strict: anything unexpected raises `ProfileError`."""
    data = _dict(raw, "profile")
    if data.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ProfileError(f"unsupported schema_version {data.get('schema_version')!r}")
    if data.get("unit") != "kW":
        raise ProfileError("unit must be kW")
    boundary = _dict(data.get("boundary"), "boundary")
    if boundary.get("measure") != "house_ac_without_ev":
        raise ProfileError("boundary.measure must be house_ac_without_ev")
    model = _dict(data.get("model"), "model")
    horizon = _dict(data.get("horizon"), "horizon")
    if horizon.get("step_minutes") != 15:
        raise ProfileError("horizon.step_minutes must be 15")
    start = _time(horizon.get("start"), "horizon.start")
    end = _time(horizon.get("end"), "horizon.end")
    if end <= start or (end - start) % PROFILE_STEP:
        raise ProfileError("horizon must be a positive whole number of 15-minute steps")
    generated_at = _time(data.get("generated_at"), "generated_at")
    valid_until = _time(data.get("valid_until"), "valid_until")
    if valid_until <= generated_at:
        raise ProfileError("valid_until must be after generated_at")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raise ProfileError("steps: expected a list")
    expected = int((end - start) / PROFILE_STEP)
    if len(raw_steps) != expected:
        raise ProfileError(f"steps: expected {expected} contiguous steps, got {len(raw_steps)}")
    steps = []
    for i, item in enumerate(raw_steps):
        item = _dict(item, f"steps[{i}]")
        t = _time(item.get("t"), f"steps[{i}].t")
        if t != start + i * PROFILE_STEP:
            raise ProfileError(f"steps[{i}].t: steps must be contiguous from horizon.start")
        p10, p50, p90 = (_kw(item.get(k), f"steps[{i}].{k}") for k in ("p10", "p50", "p90"))
        if not p10 <= p50 <= p90:
            raise ProfileError(f"steps[{i}]: expected p10 <= p50 <= p90")
        steps.append(ProfileStep(t, p10, p50, p90))

    q = _dict(data.get("quality"), "quality")
    status = q.get("status")
    if status not in ("ok", "degraded", "invalid"):
        raise ProfileError("quality.status: expected ok|degraded|invalid")
    season = q.get("season_coverage")
    if season not in ("ok", "extrapolated", "unknown"):
        raise ProfileError("quality.season_coverage: expected ok|extrapolated|unknown")
    notes = q.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise ProfileError("quality.notes: expected a list of strings")
    quality = ProfileQuality(
        status=status,
        history_start=_time(q.get("history_start"), "quality.history_start"),
        history_end=_time(q.get("history_end"), "quality.history_end"),
        days_used=_int(q.get("days_used"), "quality.days_used"),
        min_samples_per_step=_int(q.get("min_samples_per_step"), "quality.min_samples_per_step"),
        history_quarters_expected=_int(
            q.get("history_quarters_expected"), "quality.history_quarters_expected"
        ),
        history_quarters_used=_int(q.get("history_quarters_used"), "quality.history_quarters_used"),
        season_coverage=season,
        notes=tuple(notes),
    )
    if not 0 <= quality.history_quarters_used <= quality.history_quarters_expected:
        raise ProfileError("quality: expected 0 <= history_quarters_used <= expected")
    return HouseLoadProfile(
        profile_id=_str(data.get("profile_id"), "profile_id"),
        model_name=_str(model.get("name"), "model.name"),
        model_version=_str(model.get("version"), "model.version"),
        topology_epoch=_int(data.get("topology_epoch"), "topology_epoch"),
        generated_at=generated_at,
        valid_until=valid_until,
        horizon_start=start,
        horizon_end=end,
        quality=quality,
        steps=tuple(steps),
    )


def wait_pv_blocker(
    profile: HouseLoadProfile | None,
    *,
    now: datetime,
    required_end: datetime,
    topology_epoch: int,
) -> str | None:
    """Why this profile may NOT authorize waiting for PV; `None` = it may."""
    if profile is None:
        return "profile_missing"
    if profile.topology_epoch != topology_epoch:
        return "profile_topology_mismatch"
    if not profile.generated_at <= now < profile.valid_until:
        return "profile_not_current"
    if profile.horizon_start > now or profile.horizon_end < required_end:
        return "profile_horizon_short"
    if profile.quality.status != "ok":
        return f"profile_quality_{profile.quality.status}"
    if profile.quality.season_coverage != "ok":
        return f"profile_season_{profile.quality.season_coverage}"
    return None
