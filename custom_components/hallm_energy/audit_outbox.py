"""Durable outbox of audit events: delivery over MQTT, removal only on acknowledgement.

The outbox IS `StoreState.pending_audit` — it is persisted with every state commit, so
a crash or restart loses nothing and every pending event is simply delivered again
(at least once; the receiver deduplicates on `event_id`). An event leaves the outbox
ONLY when the receiver acknowledged its id; a broker acknowledgement (QoS) is not one.

* Protections and grants never wait for the receiver — delivery runs after the cycle.
* Capacity: when `AUDIT_CAPACITY` events are pending no NEW grant is started (the
  local record of its decisions could not be kept); nothing is dropped silently and
  nothing already running is stopped. Reaching `WARN_RATIO` of it is reported.
* Without an audit topic the outbox is off: events are not kept, which the status
  sensor says explicitly (`audit: disabled`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import uuid4

from .energy_core.audit import (
    MAX_BATCH_EVENTS,
    AuditBatch,
    AuditError,
    AuditEvent,
    parse_ack,
)
from .lifecycle import ack_audit
from .runtime import EnergyRuntime
from .store import StoreNotDurableError

_LOGGER = logging.getLogger(__name__)

AUDIT_CAPACITY = 500
WARN_RATIO = 0.8
BATCH_SIZE = min(20, MAX_BATCH_EVENTS)
RESEND_AFTER = timedelta(minutes=5)

Publish = Callable[[str, str], Awaitable[None]]  # (topic, payload)


class AuditOutbox:
    def __init__(self, runtime: EnergyRuntime, topic: str | None, publish: Publish) -> None:
        self.runtime = runtime
        self.topic = topic
        self._publish = publish
        self._sent_at: dict[str, datetime] = {}
        self.last_error: str | None = None
        self.last_ack_at: datetime | None = None
        self.delivered_batches = 0

    @property
    def enabled(self) -> bool:
        return self.topic is not None

    @property
    def ack_topic(self) -> str | None:
        return None if self.topic is None else f"{self.topic}/ack"

    @property
    def pending(self) -> int:
        state = self.runtime.state
        return 0 if state is None else len(state.pending_audit)

    @property
    def full(self) -> bool:
        return self.enabled and self.pending >= AUDIT_CAPACITY

    @property
    def label(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.full:
            return "full"
        return "warning" if self.pending >= AUDIT_CAPACITY * WARN_RATIO else "ok"

    async def async_deliver(self, now: datetime) -> None:
        """Send every event that has not been sent yet, or not acknowledged within
        `RESEND_AFTER`. Called after the control cycle; never raises."""
        state = self.runtime.state
        if state is None or self.runtime.blocked_reason is not None or not state.pending_audit:
            return
        if not self.enabled:
            await self._drop_local(now)
            return
        known = {e.event_id for e in state.pending_audit}
        self._sent_at = {k: v for k, v in self._sent_at.items() if k in known}
        due = [e for e in state.pending_audit
               if e.event_id not in self._sent_at
               or now - self._sent_at[e.event_id] >= RESEND_AFTER][:BATCH_SIZE]
        if not due:
            return
        batch = AuditBatch(
            batch_id=uuid4().hex,
            sent_at=now,
            events=tuple(AuditEvent(e.event_id, e.created_at, e.payload) for e in due),
        )
        try:
            await self._publish(self.topic, json.dumps(batch.to_dict(), separators=(",", ":")))
        except Exception as exc:  # MQTT down etc.: the events stay, delivery is retried
            self.last_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("audit delivery failed: %s", self.last_error)
            return
        for event in due:
            self._sent_at[event.event_id] = now
        self.delivered_batches += 1
        self.last_error = None

    async def async_handle_ack(self, payload: str | bytes, now: datetime) -> None:
        """Remove exactly the acknowledged events that are in the outbox."""
        try:
            ids = parse_ack(json.loads(payload))
        except (ValueError, AuditError) as exc:
            self.last_error = f"ack invalid: {exc}"
            return
        state = self.runtime.state
        if state is None:
            return
        mine = {e.event_id for e in state.pending_audit} & ids
        if not mine:
            return
        await self._commit(ack_audit(state, mine))
        self._sent_at = {k: v for k, v in self._sent_at.items() if k not in mine}
        self.last_ack_at = now

    async def _drop_local(self, now: datetime) -> None:
        """Audit is off: the events are not kept (said so in the status sensor)."""
        state = self.runtime.state
        assert state is not None
        await self._commit(ack_audit(state, {e.event_id for e in state.pending_audit}))
        self.last_ack_at = now

    async def _commit(self, new_state) -> None:
        try:
            await self.runtime.async_commit(new_state)
        except StoreNotDurableError as exc:
            self.last_error = f"state not persisted: {exc}"
