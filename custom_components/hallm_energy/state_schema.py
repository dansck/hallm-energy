"""Store state schema v1 — the validated shape persisted via
`LocalStateStore`. Pure: no HA imports, no I/O, no business decisions —
only types, validation and serialization of what a Store snapshot must
contain.

Nothing here decides when a grant should move from `prepared` to `active`;
it only says what a valid snapshot of any such state looks like. The
lifecycle reducer is a separate module.

Times are stored as UTC epoch seconds (unambiguous, compact) and modeled
as timezone-aware `datetime` internally — the same convention already used
for grant payloads elsewhere in this project."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

ProvisioningState = Literal["uninitialized", "ready", "blocked"]
GrantStatus = Literal["prepared", "active", "paused", "closing"]
EpisodeState = Literal["open", "closed"]
ReconcileValue = Literal["required", "running", "ready", "blocked"]
GrantPurpose = Literal["recovery", "econ"]
OperationStatus = Literal["sent", "confirmed", "unknown", "failed"]

_PROVISIONING_STATES = ("uninitialized", "ready", "blocked")
_GRANT_STATUSES = ("prepared", "active", "paused", "closing")
_EPISODE_STATES = ("open", "closed")
_RECONCILE_VALUES = ("required", "running", "ready", "blocked")
_GRANT_PURPOSES = ("recovery", "econ")
_OPERATION_STATUSES = ("sent", "confirmed", "unknown", "failed")


class StoreStateError(ValueError):
    """Invalid or ambiguous Store content — damaged or incomplete state must
    never silently pass as a valid grant."""


def _epoch(ts: datetime) -> int:
    return int(ts.timestamp())


def _parse_epoch(value: Any, where: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StoreStateError(f"{where}: ocekavan epoch cas (cislo)")
    return datetime.fromtimestamp(value, tz=UTC)


def _parse_optional_epoch(value: Any, where: str) -> datetime | None:
    return None if value is None else _parse_epoch(value, where)


def _require_number(value: Any, where: str, *, allow_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StoreStateError(f"{where}: ocekavano cislo")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise StoreStateError(f"{where}: NaN/Inf neni platna hodnota")
    if not allow_negative and number < 0:
        raise StoreStateError(f"{where}: ocekavana nezaporna hodnota")
    return number


def _require_optional_number(
    value: Any, where: str, *, allow_negative: bool = False
) -> float | None:
    return None if value is None else _require_number(value, where, allow_negative=allow_negative)


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise StoreStateError(f"{where}: ocekavan neprazdny retezec")
    return value


def _require_optional_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, where)


def _require_enum(value: Any, allowed: tuple[str, ...], where: str) -> str:
    if value not in allowed:
        raise StoreStateError(f"{where}: ocekavano jedno z {allowed}, je {value!r}")
    return value


def _require_int(value: Any, where: str, *, allow_negative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreStateError(f"{where}: ocekavano cele cislo")
    if not allow_negative and value < 0:
        raise StoreStateError(f"{where}: ocekavano nezaporne cele cislo")
    return value


def _require_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StoreStateError(f"{where}: ocekavana mapa")
    return value


# --- GrantWindow -----------------------------------------------------------------------


@dataclass(frozen=True)
class GrantWindow:
    """Polouzavrene `[start, end)` — zmena okna nesmi menit identitu/budget/checkpoint."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise StoreStateError("window: end musi byt po start (polouzavrene [start, end))")

    def to_dict(self) -> dict[str, int]:
        return {"start": _epoch(self.start), "end": _epoch(self.end)}

    @staticmethod
    def from_dict(raw: Any, where: str) -> GrantWindow:
        raw = _require_dict(raw, where)
        return GrantWindow(
            start=_parse_epoch(raw.get("start"), f"{where}.start"),
            end=_parse_epoch(raw.get("end"), f"{where}.end"),
        )


# --- ActiveGrant -------------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveGrantIdentity:
    """Nemenna od vzniku grantu — zmena okna/statusu ji nesmi zasahnout."""

    grant_id: str
    episode_id: str
    purpose: GrantPurpose
    created_at: datetime
    target_pct: float
    budget_dc_kwh: float
    counter_id: str
    counter_epoch: str
    checkpoint_dc_kwh: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "episode_id": self.episode_id,
            "purpose": self.purpose,
            "created_at": _epoch(self.created_at),
            "target_pct": self.target_pct,
            "budget_dc_kwh": self.budget_dc_kwh,
            "counter_id": self.counter_id,
            "counter_epoch": self.counter_epoch,
            "checkpoint_dc_kwh": self.checkpoint_dc_kwh,
        }

    @staticmethod
    def from_dict(raw: Any, where: str) -> ActiveGrantIdentity:
        raw = _require_dict(raw, where)
        return ActiveGrantIdentity(
            grant_id=_require_str(raw.get("grant_id"), f"{where}.grant_id"),
            episode_id=_require_str(raw.get("episode_id"), f"{where}.episode_id"),
            purpose=_require_enum(raw.get("purpose"), _GRANT_PURPOSES, f"{where}.purpose"),
            created_at=_parse_epoch(raw.get("created_at"), f"{where}.created_at"),
            target_pct=_require_number(raw.get("target_pct"), f"{where}.target_pct"),
            budget_dc_kwh=_require_number(raw.get("budget_dc_kwh"), f"{where}.budget_dc_kwh"),
            counter_id=_require_str(raw.get("counter_id"), f"{where}.counter_id"),
            counter_epoch=_require_str(raw.get("counter_epoch"), f"{where}.counter_epoch"),
            checkpoint_dc_kwh=_require_number(
                raw.get("checkpoint_dc_kwh"), f"{where}.checkpoint_dc_kwh"
            ),
        )


@dataclass(frozen=True)
class GrantUsage:
    """`counter_changed_at` is the time of the counter's last ACTUAL change,
    not of every poll — the battery counter updates in batches (~10 min), so
    polling time would make normal batch increments look implausible."""

    used_dc_kwh: float
    counter_changed_at: datetime | None
    last_trusted_raw_dc_kwh: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_dc_kwh": self.used_dc_kwh,
            "counter_changed_at": None
            if self.counter_changed_at is None
            else _epoch(self.counter_changed_at),
            "last_trusted_raw_dc_kwh": self.last_trusted_raw_dc_kwh,
        }

    @staticmethod
    def from_dict(raw: Any, where: str) -> GrantUsage:
        raw = _require_dict(raw, where)
        return GrantUsage(
            used_dc_kwh=_require_number(
                raw.get("used_dc_kwh"), f"{where}.used_dc_kwh", allow_negative=False
            ),
            counter_changed_at=_parse_optional_epoch(
                raw.get("counter_changed_at"), f"{where}.counter_changed_at"
            ),
            last_trusted_raw_dc_kwh=_require_optional_number(
                raw.get("last_trusted_raw_dc_kwh"), f"{where}.last_trusted_raw_dc_kwh"
            ),
        )


@dataclass(frozen=True)
class ActiveGrant:
    identity: ActiveGrantIdentity
    window: GrantWindow
    status: GrantStatus
    reason_code: str | None
    status_changed_at: datetime
    usage: GrantUsage
    created_policy_version: str
    plan_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "window": self.window.to_dict(),
            "status": self.status,
            "reason_code": self.reason_code,
            "status_changed_at": _epoch(self.status_changed_at),
            "usage": self.usage.to_dict(),
            "created_policy_version": self.created_policy_version,
            "plan_ref": self.plan_ref,
        }

    @staticmethod
    def from_dict(raw: Any, where: str) -> ActiveGrant:
        raw = _require_dict(raw, where)
        return ActiveGrant(
            identity=ActiveGrantIdentity.from_dict(raw.get("identity"), f"{where}.identity"),
            window=GrantWindow.from_dict(raw.get("window"), f"{where}.window"),
            status=_require_enum(raw.get("status"), _GRANT_STATUSES, f"{where}.status"),
            reason_code=_require_optional_str(raw.get("reason_code"), f"{where}.reason_code"),
            status_changed_at=_parse_epoch(
                raw.get("status_changed_at"), f"{where}.status_changed_at"
            ),
            usage=GrantUsage.from_dict(raw.get("usage"), f"{where}.usage"),
            created_policy_version=_require_str(
                raw.get("created_policy_version"), f"{where}.created_policy_version"
            ),
            plan_ref=_require_optional_str(raw.get("plan_ref"), f"{where}.plan_ref"),
        )


# --- EpisodeLedgerEntry ------------------------------------------------------------------


@dataclass(frozen=True)
class EpisodeLedgerEntry:
    episode_id: str
    purpose: GrantPurpose
    state: EpisodeState
    original_grant_id: str
    original_budget_dc_kwh: float
    final_used_dc_kwh: float | None = None
    close_reason: str | None = None
    close_at: datetime | None = None
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state == "closed" and (self.final_used_dc_kwh is None or self.close_at is None):
            raise StoreStateError(
                f"episode {self.episode_id}: closed vyzaduje final_used_dc_kwh i close_at"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "purpose": self.purpose,
            "state": self.state,
            "original_grant_id": self.original_grant_id,
            "original_budget_dc_kwh": self.original_budget_dc_kwh,
            "final_used_dc_kwh": self.final_used_dc_kwh,
            "close_reason": self.close_reason,
            "close_at": None if self.close_at is None else _epoch(self.close_at),
            "deadline_at": None if self.deadline_at is None else _epoch(self.deadline_at),
        }

    @staticmethod
    def from_dict(raw: Any, where: str) -> EpisodeLedgerEntry:
        raw = _require_dict(raw, where)
        return EpisodeLedgerEntry(
            episode_id=_require_str(raw.get("episode_id"), f"{where}.episode_id"),
            purpose=_require_enum(raw.get("purpose"), _GRANT_PURPOSES, f"{where}.purpose"),
            state=_require_enum(raw.get("state"), _EPISODE_STATES, f"{where}.state"),
            original_grant_id=_require_str(
                raw.get("original_grant_id"), f"{where}.original_grant_id"
            ),
            original_budget_dc_kwh=_require_number(
                raw.get("original_budget_dc_kwh"), f"{where}.original_budget_dc_kwh"
            ),
            final_used_dc_kwh=_require_optional_number(
                raw.get("final_used_dc_kwh"), f"{where}.final_used_dc_kwh"
            ),
            close_reason=_require_optional_str(raw.get("close_reason"), f"{where}.close_reason"),
            close_at=_parse_optional_epoch(raw.get("close_at"), f"{where}.close_at"),
            deadline_at=_parse_optional_epoch(raw.get("deadline_at"), f"{where}.deadline_at"),
        )


# --- CounterState / PlanCursor / PendingStep / ReconcileState / PendingAuditEvent --------


@dataclass(frozen=True)
class CounterState:
    counter_id: str
    counter_epoch: str
    last_trusted_raw_dc_kwh: float | None
    last_changed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "counter_id": self.counter_id,
            "counter_epoch": self.counter_epoch,
            "last_trusted_raw_dc_kwh": self.last_trusted_raw_dc_kwh,
            "last_changed_at": None
            if self.last_changed_at is None
            else _epoch(self.last_changed_at),
        }

    @staticmethod
    def from_dict(raw: Any, where: str) -> CounterState:
        raw = _require_dict(raw, where)
        return CounterState(
            counter_id=_require_str(raw.get("counter_id"), f"{where}.counter_id"),
            counter_epoch=_require_str(raw.get("counter_epoch"), f"{where}.counter_epoch"),
            last_trusted_raw_dc_kwh=_require_optional_number(
                raw.get("last_trusted_raw_dc_kwh"), f"{where}.last_trusted_raw_dc_kwh"
            ),
            last_changed_at=_parse_optional_epoch(
                raw.get("last_changed_at"), f"{where}.last_changed_at"
            ),
        )


@dataclass(frozen=True)
class PlanCursor:
    """Plan producer identity + last accepted epoch — a producer reboot must
    not be accepted as a legitimate reset."""

    producer_id: str
    last_accepted_epoch: int

    def to_dict(self) -> dict[str, Any]:
        return {"producer_id": self.producer_id, "last_accepted_epoch": self.last_accepted_epoch}

    @staticmethod
    def from_dict(raw: Any, where: str) -> PlanCursor:
        raw = _require_dict(raw, where)
        return PlanCursor(
            producer_id=_require_str(raw.get("producer_id"), f"{where}.producer_id"),
            last_accepted_epoch=_require_int(
                raw.get("last_accepted_epoch"), f"{where}.last_accepted_epoch"
            ),
        )


@dataclass(frozen=True)
class PendingStep:
    step_id: str
    grant_id: str
    episode_id: str
    observed_before: dict[str, Any]
    requested_changes: dict[str, Any]
    operation_order: tuple[str, ...]
    safety_revision: int
    operation_status: dict[str, OperationStatus]

    def __post_init__(self) -> None:
        unknown = set(self.operation_status) - set(self.operation_order)
        if unknown:
            raise StoreStateError(f"pending_step {self.step_id}: operation_status zna {unknown}")
        for op, status in self.operation_status.items():
            if status not in _OPERATION_STATUSES:
                raise StoreStateError(f"pending_step {self.step_id}.{op}: neznamy stav {status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "grant_id": self.grant_id,
            "episode_id": self.episode_id,
            "observed_before": self.observed_before,
            "requested_changes": self.requested_changes,
            "operation_order": list(self.operation_order),
            "safety_revision": self.safety_revision,
            "operation_status": self.operation_status,
        }

    @staticmethod
    def from_dict(raw: Any, where: str) -> PendingStep:
        raw = _require_dict(raw, where)
        operation_order_raw = raw.get("operation_order")
        if not isinstance(operation_order_raw, list) or not all(
            isinstance(x, str) for x in operation_order_raw
        ):
            raise StoreStateError(f"{where}.operation_order: ocekavan seznam retezcu")
        return PendingStep(
            step_id=_require_str(raw.get("step_id"), f"{where}.step_id"),
            grant_id=_require_str(raw.get("grant_id"), f"{where}.grant_id"),
            episode_id=_require_str(raw.get("episode_id"), f"{where}.episode_id"),
            observed_before=_require_dict(raw.get("observed_before"), f"{where}.observed_before"),
            requested_changes=_require_dict(
                raw.get("requested_changes"), f"{where}.requested_changes"
            ),
            operation_order=tuple(operation_order_raw),
            safety_revision=_require_int(raw.get("safety_revision"), f"{where}.safety_revision"),
            operation_status=_require_dict(
                raw.get("operation_status"), f"{where}.operation_status"
            ),
        )


@dataclass(frozen=True)
class ReconcileState:
    value: ReconcileValue
    reason_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "reason_code": self.reason_code}

    @staticmethod
    def from_dict(raw: Any, where: str) -> ReconcileState:
        raw = _require_dict(raw, where)
        return ReconcileState(
            value=_require_enum(raw.get("value"), _RECONCILE_VALUES, f"{where}.value"),
            reason_code=_require_optional_str(raw.get("reason_code"), f"{where}.reason_code"),
        )


@dataclass(frozen=True)
class PendingAuditEvent:
    """`event_id` must be stable — the outbox deduplicates on it when a
    delivery is retried after a crash."""

    event_id: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "payload": self.payload}

    @staticmethod
    def from_dict(raw: Any, where: str) -> PendingAuditEvent:
        raw = _require_dict(raw, where)
        return PendingAuditEvent(
            event_id=_require_str(raw.get("event_id"), f"{where}.event_id"),
            payload=_require_dict(raw.get("payload"), f"{where}.payload"),
        )


# --- StoreState (root) -------------------------------------------------------------------


@dataclass(frozen=True)
class StoreState:
    state_revision: int
    policy_version: str
    core_version: str
    installation_id: str
    provisioning_state: ProvisioningState
    episode_ledger: tuple[EpisodeLedgerEntry, ...]
    active_grant: ActiveGrant | None
    counter_state: CounterState
    plan_cursor: PlanCursor | None
    pending_step: PendingStep | None
    reconcile_state: ReconcileState
    pending_audit: tuple[PendingAuditEvent, ...]

    def __post_init__(self) -> None:
        if self.active_grant is None:
            return
        identity = self.active_grant.identity
        if identity.counter_id != self.counter_state.counter_id:
            raise StoreStateError(
                "active_grant.identity.counter_id neodpovida counter_state.counter_id"
            )
        if identity.counter_epoch != self.counter_state.counter_epoch:
            raise StoreStateError(
                "active_grant.identity.counter_epoch neodpovida counter_state.counter_epoch"
            )
        matching_episode = next(
            (e for e in self.episode_ledger if e.episode_id == identity.episode_id), None
        )
        if matching_episode is None:
            raise StoreStateError(
                f"active_grant.identity.episode_id '{identity.episode_id}'"
                " nema odpovidajici zaznam v episode_ledger"
            )
        if matching_episode.state != "open":
            raise StoreStateError(
                f"active_grant odkazuje na episode '{identity.episode_id}', ktera uz je closed"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_revision": self.state_revision,
            "policy_version": self.policy_version,
            "core_version": self.core_version,
            "installation_id": self.installation_id,
            "provisioning_state": self.provisioning_state,
            "episode_ledger": [e.to_dict() for e in self.episode_ledger],
            "active_grant": None if self.active_grant is None else self.active_grant.to_dict(),
            "counter_state": self.counter_state.to_dict(),
            "plan_cursor": None if self.plan_cursor is None else self.plan_cursor.to_dict(),
            "pending_step": None if self.pending_step is None else self.pending_step.to_dict(),
            "reconcile_state": self.reconcile_state.to_dict(),
            "pending_audit": [e.to_dict() for e in self.pending_audit],
        }


def parse_store_state(raw: Any) -> StoreState:
    """Validate the `data` loaded from Store as a whole — no silent defaults
    for missing identity, checkpoint, usage or closure."""
    raw = _require_dict(raw, "store_state")
    episode_ledger_raw = raw.get("episode_ledger")
    if not isinstance(episode_ledger_raw, list):
        raise StoreStateError("episode_ledger: ocekavan seznam")
    episode_ledger = tuple(
        EpisodeLedgerEntry.from_dict(e, f"episode_ledger[{i}]")
        for i, e in enumerate(episode_ledger_raw)
    )
    pending_audit_raw = raw.get("pending_audit")
    if not isinstance(pending_audit_raw, list):
        raise StoreStateError("pending_audit: ocekavan seznam")
    pending_audit = tuple(
        PendingAuditEvent.from_dict(e, f"pending_audit[{i}]")
        for i, e in enumerate(pending_audit_raw)
    )
    active_grant_raw = raw.get("active_grant")
    plan_cursor_raw = raw.get("plan_cursor")
    pending_step_raw = raw.get("pending_step")
    return StoreState(
        state_revision=_require_int(raw.get("state_revision"), "state_revision"),
        policy_version=_require_str(raw.get("policy_version"), "policy_version"),
        core_version=_require_str(raw.get("core_version"), "core_version"),
        installation_id=_require_str(raw.get("installation_id"), "installation_id"),
        provisioning_state=_require_enum(
            raw.get("provisioning_state"), _PROVISIONING_STATES, "provisioning_state"
        ),
        episode_ledger=episode_ledger,
        active_grant=None
        if active_grant_raw is None
        else ActiveGrant.from_dict(active_grant_raw, "active_grant"),
        counter_state=CounterState.from_dict(raw.get("counter_state"), "counter_state"),
        plan_cursor=None
        if plan_cursor_raw is None
        else PlanCursor.from_dict(plan_cursor_raw, "plan_cursor"),
        pending_step=None
        if pending_step_raw is None
        else PendingStep.from_dict(pending_step_raw, "pending_step"),
        reconcile_state=ReconcileState.from_dict(raw.get("reconcile_state"), "reconcile_state"),
        pending_audit=pending_audit,
    )
