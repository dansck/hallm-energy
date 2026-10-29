"""Notifications to the owner through a Home Assistant `notify.*` service.

Edge-triggered with a repeat: a condition notifies when it becomes active and again after
`REPEAT_AFTER` while it stays active; when it clears it re-arms, so the next onset notifies
at once. A notification never blocks or breaks the control loop — the call has a hard timeout
and any failure is only logged. Without a configured service nothing is sent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

REPEAT_AFTER = timedelta(hours=6)
CALL_TIMEOUT_S = 10.0
TITLE = "Hallm Energy"
SERVICE_RE = re.compile(r"^notify\.[a-z0-9_]+$")


class Notifier:
    def __init__(self, hass: HomeAssistant, service: str | None) -> None:
        self.hass = hass
        self.service = service
        self._sent_at: dict[str, datetime] = {}
        self.last_error: str | None = None
        self.sent = 0

    @property
    def enabled(self) -> bool:
        return self.service is not None

    async def async_observe(self, kind: str, active: bool, message: str,
                            now: datetime | None = None) -> bool:
        """Report the current truth of one condition; returns whether a message was sent."""
        if not active:
            self._sent_at.pop(kind, None)
            return False
        now = now or dt_util.utcnow()
        last = self._sent_at.get(kind)
        if last is not None and now - last < REPEAT_AFTER:
            return False
        self._sent_at[kind] = now  # set before the attempt: a failing service is not hammered
        return await self._send(message)

    async def async_notify_once(self, kind: str, message: str, now: datetime | None = None,
                                min_interval: timedelta = timedelta(minutes=10)) -> bool:
        """An event (not a lasting condition), e.g. a safety stop: at most once per interval."""
        now = now or dt_util.utcnow()
        last = self._sent_at.get(kind)
        if last is not None and now - last < min_interval:
            return False
        self._sent_at[kind] = now
        return await self._send(message)

    async def _send(self, message: str) -> bool:
        if self.service is None:
            return False
        domain, name = self.service.split(".", 1)
        try:
            async with asyncio.timeout(CALL_TIMEOUT_S):
                await self.hass.services.async_call(
                    domain, name, {"title": TITLE, "message": message}, blocking=True)
        except Exception as exc:  # a missing/failing service must never touch the control loop
            self.last_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("notification failed: %s", self.last_error)
            return False
        self.last_error = None
        self.sent += 1
        return True
