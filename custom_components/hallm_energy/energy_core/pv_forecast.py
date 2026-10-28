"""PV production forecast snapshot (schema v1): structure, strict parsing and
the receiver-side check whether PV may be credited.

`kw` is the forecast AC production of all PV branches with the calibration
correction already applied (exactly once, by the producer). Freshness is
judged by `retrieved_at` — when the data was obtained — not by the age of
the weather model; `source_issued_at` is set only when the source states it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

PV_SCHEMA_VERSION = 1
PV_STEP = timedelta(minutes=15)
RETRIEVED_FUTURE_TOLERANCE = timedelta(minutes=5)

PvQuality = Literal["ok", "degraded", "invalid"]


class PvForecastError(ValueError):
    """Malformed PV forecast payload."""


@dataclass(frozen=True)
class PvStep:
    t: datetime
    kw: float
    kw_model: float


@dataclass(frozen=True)
class PvForecast:
    snapshot_id: str
    model_name: str
    model_version: str
    topology_epoch: int
    retrieved_at: datetime
    source_issued_at: datetime | None
    valid_until: datetime
    provider: str
    weather_model: str | None
    horizon_start: datetime
    horizon_end: datetime
    branches: tuple[str, ...]
    corrections: dict[str, float]
    quality: PvQuality
    notes: tuple[str, ...]
    steps: tuple[PvStep, ...]

    def kw_at(self, at: datetime) -> float | None:
        """Corrected AC production of the step containing `at`; `None` outside."""
        if not self.horizon_start <= at < self.horizon_end:
            return None
        return self.steps[int((at - self.horizon_start) / PV_STEP)].kw

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PV_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "model": {"name": self.model_name, "version": self.model_version},
            "topology_epoch": self.topology_epoch,
            "retrieved_at": _iso(self.retrieved_at),
            "source_issued_at": None if self.source_issued_at is None
            else _iso(self.source_issued_at),
            "valid_until": _iso(self.valid_until),
            "source": {"provider": self.provider, "weather_model": self.weather_model},
            "horizon": {"start": _iso(self.horizon_start), "end": _iso(self.horizon_end),
                        "step_minutes": 15},
            "unit": "kW",
            "boundary": {"measure": "pv_ac_total", "branches": list(self.branches)},
            "corrections": dict(self.corrections),
            "quality": {"status": self.quality, "notes": list(self.notes)},
            "steps": [{"t": _iso(s.t), "kw": s.kw, "kw_model": s.kw_model} for s in self.steps],
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _time(raw: Any, where: str) -> datetime:
    if not isinstance(raw, str):
        raise PvForecastError(f"{where}: expected an ISO time string")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise PvForecastError(f"{where}: invalid time {raw!r}") from None
    if value.tzinfo is None:
        raise PvForecastError(f"{where}: time without offset")
    return value.astimezone(UTC)


def _dict(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PvForecastError(f"{where}: expected an object")
    return raw


def _str(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise PvForecastError(f"{where}: expected a non-empty string")
    return raw


def _number(raw: Any, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise PvForecastError(f"{where}: expected a number")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise PvForecastError(f"{where}: expected a finite value >= 0")
    return value


def parse_pv_forecast(raw: Any) -> PvForecast:
    """Strict: anything unexpected raises `PvForecastError`."""
    data = _dict(raw, "pv_forecast")
    if data.get("schema_version") != PV_SCHEMA_VERSION:
        raise PvForecastError(f"unsupported schema_version {data.get('schema_version')!r}")
    if data.get("unit") != "kW":
        raise PvForecastError("unit must be kW")
    boundary = _dict(data.get("boundary"), "boundary")
    if boundary.get("measure") != "pv_ac_total":
        raise PvForecastError("boundary.measure must be pv_ac_total")
    branches = boundary.get("branches")
    if not isinstance(branches, list) or not branches or not all(
        isinstance(b, str) and b for b in branches
    ):
        raise PvForecastError("boundary.branches: expected a non-empty list of names")
    model = _dict(data.get("model"), "model")
    source = _dict(data.get("source"), "source")
    horizon = _dict(data.get("horizon"), "horizon")
    if horizon.get("step_minutes") != 15:
        raise PvForecastError("horizon.step_minutes must be 15")
    start = _time(horizon.get("start"), "horizon.start")
    end = _time(horizon.get("end"), "horizon.end")
    if end <= start or (end - start) % PV_STEP:
        raise PvForecastError("horizon must be a positive whole number of 15-minute steps")
    retrieved = _time(data.get("retrieved_at"), "retrieved_at")
    valid_until = _time(data.get("valid_until"), "valid_until")
    if valid_until <= retrieved:
        raise PvForecastError("valid_until must be after retrieved_at")
    issued_raw = data.get("source_issued_at")
    issued = None if issued_raw is None else _time(issued_raw, "source_issued_at")
    if issued is not None and issued > retrieved:
        raise PvForecastError("source_issued_at cannot be after retrieved_at")
    corrections = _dict(data.get("corrections"), "corrections")
    corrections = {_str(k, "corrections key"): _number(v, f"corrections.{k}")
                   for k, v in corrections.items()}
    weather_model = source.get("weather_model")
    if weather_model is not None:
        weather_model = _str(weather_model, "source.weather_model")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raise PvForecastError("steps: expected a list")
    expected = int((end - start) / PV_STEP)
    if len(raw_steps) != expected:
        raise PvForecastError(f"steps: expected {expected} contiguous steps, got {len(raw_steps)}")
    steps = []
    for i, item in enumerate(raw_steps):
        item = _dict(item, f"steps[{i}]")
        t = _time(item.get("t"), f"steps[{i}].t")
        if t != start + i * PV_STEP:
            raise PvForecastError(f"steps[{i}].t: steps must be contiguous from horizon.start")
        kw = _number(item.get("kw"), f"steps[{i}].kw")
        kw_model = _number(item.get("kw_model"), f"steps[{i}].kw_model")
        if kw > kw_model + 1e-9:
            raise PvForecastError(f"steps[{i}]: corrected kw cannot exceed kw_model")
        steps.append(PvStep(t, kw, kw_model))

    quality = _dict(data.get("quality"), "quality")
    status = quality.get("status")
    if status not in ("ok", "degraded", "invalid"):
        raise PvForecastError("quality.status: expected ok|degraded|invalid")
    notes = quality.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise PvForecastError("quality.notes: expected a list of strings")
    epoch = data.get("topology_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise PvForecastError("topology_epoch: expected an integer")
    return PvForecast(
        snapshot_id=_str(data.get("snapshot_id"), "snapshot_id"),
        model_name=_str(model.get("name"), "model.name"),
        model_version=_str(model.get("version"), "model.version"),
        topology_epoch=epoch,
        retrieved_at=retrieved,
        source_issued_at=issued,
        valid_until=valid_until,
        provider=_str(source.get("provider"), "source.provider"),
        weather_model=weather_model,
        horizon_start=start,
        horizon_end=end,
        branches=tuple(branches),
        corrections=corrections,
        quality=status,
        notes=tuple(notes),
        steps=tuple(steps),
    )


def pv_blocker(
    forecast: PvForecast | None,
    *,
    now: datetime,
    required_end: datetime,
    topology_epoch: int,
) -> str | None:
    """Why this forecast may NOT be credited over `[now, required_end)`; `None` = it may."""
    if forecast is None:
        return "pv_forecast_missing"
    if forecast.topology_epoch != topology_epoch:
        return "pv_forecast_topology_mismatch"
    if forecast.retrieved_at > now + RETRIEVED_FUTURE_TOLERANCE:
        return "pv_forecast_retrieved_in_future"
    if not now < forecast.valid_until:
        return "pv_forecast_expired"
    if forecast.horizon_start > now or forecast.horizon_end < required_end:
        return "pv_forecast_horizon_short"
    if forecast.quality != "ok":
        return f"pv_forecast_quality_{forecast.quality}"
    return None
