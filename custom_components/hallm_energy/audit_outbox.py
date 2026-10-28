"""Durable outbox of audit events: delivery over MQTT, removal only on acknowledgement.

The outbox IS `StoreState.pending_audit` — it is persisted with every state commit, so
a crash or restart loses nothing and every pending event is simply delivered again
(at least once; the receiver deduplicates on `event_id`). An event leaves the outbox
ONLY when the receiver acknowledged its id, or when the user explicitly discards the
backlog; a broker acknowledgement (QoS) is not one.

* Delivery is its own worker, never part of the control cycle: the batch is snapshotted
  while the control lock is held briefly, the network wait (with a hard timeout) happens
  OUTSIDE the lock, and only the bookkeeping takes the lock again. A hanging broker can
  therefore not delay a control cycle, an acknowledgement, a service call or an unload.
* Capacity: when `AUDIT_CAPACITY` events are pending no NEW grant is started (the
  local record of its decisions could not be kept); nothing is dropped and nothing
  already running is stopped. Reaching `WARN_RATIO` of it is reported.
* Without an audit topic delivery is PAUSED, not abandoned: events are still created and
  kept, and are delivered once a topic is set again. Removing the backlog is a separate,
  explicit act (`discard`); switching the transport off never implies it.
"""

from __future__ import annotations

import asyncio
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
PUBLISH_TIMEOUT_S = 10.0

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
            return "paused"
        if self.full:
            return "full"
        return "warning" if self.pending >= AUDIT_CAPACITY * WARN_RATIO else "ok"

    def prepare_delivery(self, now: datetime) -> tuple[str, list[str]] | None:
        """Snapshot of the next batch: (payload, ids) or `None` when nothing is due or
        delivery is paused. Pure bookkeeping — no network, safe under the control lock."""
        state = self.runtime.state
        if (state is None or self.runtime.blocked_reason is not None
                or not state.pending_audit or not self.enabled):
            return None
        known = {e.event_id for e in state.pending_audit}
        self._sent_at = {k: v for k, v in self._sent_at.items() if k in known}
        due = [e for e in state.pending_audit
               if e.event_id not in self._sent_at
               or now - self._sent_at[e.event_id] >= RESEND_AFTER][:BATCH_SIZE]
        if not due:
            return None
        batch = AuditBatch(
            batch_id=uuid4().hex,
            sent_at=now,
            events=tuple(AuditEvent(e.event_id, e.created_at, e.payload) for e in due),
        )
        return json.dumps(batch.to_dict(), separators=(",", ":")), [e.event_id for e in due]

    def mark_sent(self, ids: list[str], now: datetime) -> None:
        for event_id in ids:
            self._sent_at[event_id] = now
        self.delivered_batches += 1
        self.last_error = None

    async def async_deliver(self, lock: asyncio.Lock, clock: Callable[[], datetime]) -> None:
        """One delivery round; never raises. The control `lock` is held only to snapshot the
        batch and to book the result, never while waiting for the broker."""
        async with lock:
            prepared = self.prepare_delivery(clock())
        if prepared is None:
            return
        payload, ids = prepared
        try:
            async with asyncio.timeout(PUBLISH_TIMEOUT_S):
                await self._publish(self.topic, payload)
        except Exception as exc:  # MQTT down, timeout, …: the events stay, delivery is retried
            self.last_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("audit delivery failed: %s", self.last_error)
            return
        async with lock:
            self.mark_sent(ids, clock())

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

    async def async_discard(self) -> int:
        """Explicit removal of the whole backlog (a user decision, never implicit)."""
        state = self.runtime.state
        if state is None or self.runtime.blocked_reason is not None:
            return 0
        count = len(state.pending_audit)
        if count:
            await self._commit(ack_audit(state, {e.event_id for e in state.pending_audit}))
            self._sent_at.clear()
        return count

    async def _commit(self, new_state) -> None:
        try:
            await self.runtime.async_commit(new_state)
        except StoreNotDurableError as exc:
            self.last_error = f"state not persisted: {exc}"
