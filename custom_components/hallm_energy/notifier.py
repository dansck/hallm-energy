"""Notifications to the owner through a Home Assistant `notify.*` service.

Only for CRITICAL events. Edge-triggered with a repeat: a condition notifies once it has been
active without a break for its `after` delay (a flicker does not count) and again after
`REPEAT_AFTER` while it stays active; when it clears it re-arms, so the next onset notifies
as soon as its delay has passed. A notification never blocks or breaks the control loop — the
call has a hard timeout and any failure is only logged. Without a configured service nothing
is sent.
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
        self._since: dict[str, datetime] = {}
        self.last_error: str | None = None
        self.sent = 0

    @property
    def enabled(self) -> bool:
        return self.service is not None

    async def async_observe(self, kind: str, active: bool, message: str,
                            now: datetime | None = None,
                            after: timedelta = timedelta(0)) -> bool:
        """Report the current truth of one condition; returns whether a message was sent."""
        if not active:
            self._sent_at.pop(kind, None)
            self._since.pop(kind, None)
            return False
        now = now or dt_util.utcnow()
        since = self._since.setdefault(kind, now)
        if now - since < after:
            return False
        last = self._sent_at.get(kind)
        if last is not None and now - last < REPEAT_AFTER:
            return False
        self._sent_at[kind] = now  # set before the attempt: a failing service is not hammered
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
