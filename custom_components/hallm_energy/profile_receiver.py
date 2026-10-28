"""Receives the retained house load profile snapshot and keeps the newest
valid one. Validation is the shared parser's; usability for waiting on PV is
decided later by `wait_pv_blocker` (current, complete, quality ok)."""

from __future__ import annotations

import json
from datetime import datetime

from .energy_core.house_load import HouseLoadProfile, ProfileError, parse_profile
from .energy_core.pv_forecast import PvForecast, PvForecastError, parse_pv_forecast


class ProfileReceiver:
    def __init__(self) -> None:
        self.profile: HouseLoadProfile | None = None
        self.error: str | None = None
        self.received_at: datetime | None = None

    def handle(self, payload: str | bytes, now: datetime) -> None:
        """An invalid or older message is rejected and recorded; the previous
        valid profile stays until its own `valid_until`."""
        self.received_at = now
        try:
            profile = parse_profile(json.loads(payload))
        except (ValueError, ProfileError) as exc:  # JSONDecodeError is a ValueError
            self.error = f"invalid: {exc}"
            return
        if self.profile is not None and profile.generated_at < self.profile.generated_at:
            self.error = "older_profile_rejected"
            return
        self.profile, self.error = profile, None


class PvForecastReceiver:
    """Same rules for the PV forecast snapshot; newer = later `retrieved_at`."""

    def __init__(self) -> None:
        self.forecast: PvForecast | None = None
        self.error: str | None = None
        self.received_at: datetime | None = None

    def handle(self, payload: str | bytes, now: datetime) -> None:
        self.received_at = now
        try:
            forecast = parse_pv_forecast(json.loads(payload))
        except (ValueError, PvForecastError) as exc:
            self.error = f"invalid: {exc}"
            return
        if self.forecast is not None and forecast.retrieved_at < self.forecast.retrieved_at:
            self.error = "older_forecast_rejected"
            return
        self.forecast, self.error = forecast, None
