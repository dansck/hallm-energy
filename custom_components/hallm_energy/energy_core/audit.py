"""Audit event transport: a durable outbox on the integration side, delivered in
batches over MQTT and removed only after an explicit acknowledgement.

Delivery is at least once. An event is identified by its `event_id` for ever; its
content never changes between deliveries (the receiver stores it idempotently and
refuses a different content under the same id). Everything the receiver needs to
interpret an event — installation, policy and core versions, mode, state revision —
is stamped into the event when it is created, never at delivery time.

Batch (integration → receiver, topic `T`):
    {"schema_version": 1, "batch_id": "...", "sent_at": "...Z",
     "events": [{"event_id": "...", "created_at": "...Z" | null, "payload": {...}}]}

Acknowledgement (receiver → integration, topic `T/ack`):
    {"schema_version": 1, "event_ids": ["..."], "acked_at": "...Z"}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

AUDIT_SCHEMA_VERSION = 1
MAX_BATCH_EVENTS = 50
MAX_EVENT_BYTES = 16_384


class AuditError(ValueError):
    """Malformed audit message."""


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    created_at: datetime | None
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "created_at": None if self.created_at is None else _iso(self.created_at),
            "payload": self.payload,
        }

    def canonical_content(self) -> str:
        """The exact text a receiver stores: deterministic, independent of the batch
        or of when it was sent — the same event always yields the same bytes."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)


@dataclass(frozen=True)
class AuditBatch:
    batch_id: str
    sent_at: datetime
    events: tuple[AuditEvent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "sent_at": _iso(self.sent_at),
            "events": [e.to_dict() for e in self.events],
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _time(raw: Any, where: str) -> datetime:
    if not isinstance(raw, str):
        raise AuditError(f"{where}: expected an ISO time string")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise AuditError(f"{where}: invalid time {raw!r}") from None
    if value.tzinfo is None:
        raise AuditError(f"{where}: time without offset")
    return value.astimezone(UTC)


def _str(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise AuditError(f"{where}: expected a non-empty string")
    return raw


def parse_event(raw: Any, where: str = "event") -> AuditEvent:
    if not isinstance(raw, dict):
        raise AuditError(f"{where}: expected an object")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise AuditError(f"{where}.payload: expected an object")
    created = raw.get("created_at")
    event = AuditEvent(
        event_id=_str(raw.get("event_id"), f"{where}.event_id"),
        created_at=None if created is None else _time(created, f"{where}.created_at"),
        payload=payload,
    )
    if len(event.canonical_content().encode("utf-8")) > MAX_EVENT_BYTES:
        raise AuditError(f"{where}: event larger than {MAX_EVENT_BYTES} bytes")
    return event


def parse_batch(raw: Any) -> AuditBatch:
    """Strict: anything unexpected raises `AuditError` and the batch is not ingested."""
    if not isinstance(raw, dict):
        raise AuditError("batch: expected an object")
    if raw.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise AuditError(f"unsupported schema_version {raw.get('schema_version')!r}")
    events_raw = raw.get("events")
    if not isinstance(events_raw, list) or not events_raw:
        raise AuditError("events: expected a non-empty list")
    if len(events_raw) > MAX_BATCH_EVENTS:
        raise AuditError(f"events: more than {MAX_BATCH_EVENTS} in one batch")
    events = tuple(parse_event(e, f"events[{i}]") for i, e in enumerate(events_raw))
    if len({e.event_id for e in events}) != len(events):
        raise AuditError("events: duplicate event_id in one batch")
    return AuditBatch(
        batch_id=_str(raw.get("batch_id"), "batch_id"),
        sent_at=_time(raw.get("sent_at"), "sent_at"),
        events=events,
    )


def build_ack(event_ids: list[str], at: datetime) -> dict[str, Any]:
    return {"schema_version": AUDIT_SCHEMA_VERSION, "event_ids": list(event_ids),
            "acked_at": _iso(at)}


def parse_ack(raw: Any) -> set[str]:
    if not isinstance(raw, dict) or raw.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise AuditError("ack: unsupported message")
    ids = raw.get("event_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        raise AuditError("ack.event_ids: expected a non-empty list of ids")
    return set(ids)
