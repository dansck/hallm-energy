"""The charge limit on the car, written apart from the wallbox.

The limit lives in the vehicle cloud and takes a while to be applied, so it has a writer of its
own: a slow confirmation must never hold up the wallbox, whose protective pause cannot wait.
It is carried out under the same gate as the wallbox writer (wallbox management on, shadow off).
The limit is only ever raised: the car puts itself back to its default when a charge is finished.
A write is confirmed by reading the value back; one attempt at a time; a failed attempt is not
repeated for `RETRY_AFTER`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

from .config import EnergyConfig
from .device_io import DeviceIO, WriteResult
from .executor import Gates
from .slot_plan import Op

_LOGGER = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 600.0  # the cloud may take minutes
RETRY_AFTER = timedelta(minutes=5)


class CarLimitWriter:
    def __init__(self, hass: HomeAssistant, config: EnergyConfig, gates: Gates,
                 io: DeviceIO | None = None) -> None:
        self.hass, self.config, self.gates = hass, config, gates
        self.io = io or DeviceIO(hass, call_timeout_s=30.0, readback_timeout_s=CONFIRM_TIMEOUT_S,
                                 poll_interval_s=5.0)
        self._task: asyncio.Task | None = None
        self._retry_at: datetime | None = None
        self.last_result: str = "idle"
        self.last_write: WriteResult | None = None

    def live(self) -> bool:
        cfg = self.config
        return bool(cfg.manage_ev and cfg.ev_limit_entity and self.gates.floors_live())

    def request(self, desired: float | None, now: datetime) -> None:
        """Ask for the limit to be `desired`; returns at once, the write runs in the background."""
        entity = self.config.ev_limit_entity
        if entity is None or desired is None:
            return
        if not self.live():
            self.last_result = "shadow"
            return
        current = self.io.read(entity)
        if current is not None and current >= desired:
            self.last_result = "in place"
            return
        if self._task is not None and not self._task.done():
            self.last_result = "busy"
            return
        if self._retry_at is not None and now < self._retry_at:
            self.last_result = "backoff"
            return
        self.last_result = "writing"
        self._task = self.hass.async_create_background_task(
            self._write(entity, desired, now), "hallm_energy EV car limit")

    async def _write(self, entity: str, desired: float, now: datetime) -> None:
        try:
            result = await self.io.write(Op(entity, desired))
        except Exception:
            _LOGGER.exception("car limit write failed")
            self.last_result, self._retry_at = "error", now + RETRY_AFTER
            return
        self.last_write = result
        _LOGGER.info("EV car limit %s=%s: %s", entity, desired, result.outcome)
        if result.outcome == "confirmed":
            self.last_result, self._retry_at = "done", None
        else:
            self.last_result, self._retry_at = result.outcome, now + RETRY_AFTER
