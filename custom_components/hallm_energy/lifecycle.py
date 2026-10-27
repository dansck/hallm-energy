"""Pure grant lifecycle over `StoreState` — no HA, no I/O, no device writes.

Every function takes a validated snapshot and returns the next one; the
caller persists it (confirmed write) BEFORE acting on the result. Grant
accounting itself (usage from the charge counter, stop conditions) comes
from the shared `energy_core.grant`; this module only maps it onto the
persisted lifecycle:

    (none) ─prepare_grant─▶ prepared
    prepared/paused ─step (proposes pending_step)─▶ record_step_result(confirmed) ─▶ active
    active ─step─▶ paused                (immediately, stopping never waits)
    any open ─step─▶ closing             (target SOC or budget backup)
    closing ─confirm_closed─▶ closed episode in the ledger, active_grant = None

`active` means "locally confirmed settings", not a guarantee of current
physical charging current.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .energy_core.grant import (
    COUNTER_STEP_TOLERANCE_KWH,
    DEFAULT_GRANT_BUDGET_FACTOR,
    DEFAULT_MAX_CHARGE_RATE_KW,
    CounterReading,
    Grant,
    GrantIdentity,
    evaluate_grant,
    grant_budget_exhausted,
    update_grant_used,
)
from .energy_core.grant import GrantWindow as CoreGrantWindow
from .state_schema import (
    ActiveGrant,
    ActiveGrantIdentity,
    CounterState,
    EpisodeLedgerEntry,
    GrantUsage,
    GrantWindow,
    OperationStatus,
    PendingAuditEvent,
    PendingStep,
    ReconcileState,
    ReconcileValue,
    StoreState,
)


class GrantTransitionError(ValueError):
    """The requested transition is not allowed from this state (caller bug,
    replay, or a state that must not authorize anything)."""


@dataclass(frozen=True)
class StepResult:
    """`charge_enable` is the desired setting now. `step_id` is set only when a
    NEW enabling write must be executed (persist `state` first, then perform
    the write, then `record_step_result`)."""

    state: StoreState
    charge_enable: bool
    reason: str | None
    step_id: str | None = None


def _bump(state: StoreState, **changes: Any) -> StoreState:
    return replace(state, state_revision=state.state_revision + 1, **changes)


def _audit(
    state: StoreState, event_id: str, payload: dict[str, Any]
) -> tuple[PendingAuditEvent, ...]:
    if any(e.event_id == event_id for e in state.pending_audit):
        return state.pending_audit
    return (*state.pending_audit, PendingAuditEvent(event_id=event_id, payload=payload))


def _core_grant(active: ActiveGrant) -> Grant:
    identity = active.identity
    return Grant(
        identity=GrantIdentity(
            id=identity.grant_id,
            purpose=identity.purpose,
            created_at=identity.created_at,
            target_pct=identity.target_pct,
            budget_dc_kwh=identity.budget_dc_kwh,
            counter_id=identity.counter_id,
            checkpoint_dc_kwh=identity.checkpoint_dc_kwh,
        ),
        window=CoreGrantWindow(start=active.window.start, end=active.window.end),
    )


def set_reconcile(state: StoreState, value: ReconcileValue, reason_code: str | None) -> StoreState:
    target = ReconcileState(value=value, reason_code=reason_code)
    if state.reconcile_state == target:
        return state
    return _bump(state, reconcile_state=target)


def on_startup(state: StoreState) -> StoreState:
    """A persisted `ready`/`active` is no proof after a restart: reconcile is
    always required before normal actions resume. A `blocked` state stays
    blocked — a restart is not a way out of it."""
    if state.reconcile_state.value == "blocked":
        return state
    return set_reconcile(state, "required", "startup")


def new_installation_state(
    *, installation_id: str, policy_version: str, core_version: str
) -> StoreState:
    """Genuine first run: nothing is authorized until explicit provisioning."""
    return StoreState(
        state_revision=0,
        policy_version=policy_version,
        core_version=core_version,
        installation_id=installation_id,
        provisioning_state="uninitialized",
        episode_ledger=(),
        active_grant=None,
        counter_state=None,
        plan_cursor=None,
        pending_step=None,
        reconcile_state=ReconcileState("required", "not_provisioned"),
        pending_audit=(),
    )


def lost_state(
    *, installation_id: str, policy_version: str, core_version: str, reason: str
) -> StoreState:
    """The installation existed but its state is gone (deleted, corrupt file
    moved aside, …). Never treated as a first run: blocked, no grant can be
    recreated from nothing; leaving it needs an explicit procedure."""
    return replace(
        new_installation_state(
            installation_id=installation_id,
            policy_version=policy_version,
            core_version=core_version,
        ),
        provisioning_state="blocked",
        reconcile_state=ReconcileState("blocked", reason),
    )


def provision(
    state: StoreState,
    *,
    counter_id: str,
    counter_epoch: str,
    trusted_raw_dc_kwh: float,
    at: datetime,
) -> StoreState:
    """`uninitialized` → `ready` after a read-only inventory verified the
    charge counter. Reconcile is still required before anything enables."""
    if state.provisioning_state != "uninitialized":
        raise GrantTransitionError(f"cannot provision from {state.provisioning_state}")
    if state.active_grant is not None:
        raise GrantTransitionError("cannot provision with an active grant")
    return _bump(
        state,
        provisioning_state="ready",
        counter_state=CounterState(
            counter_id=counter_id,
            counter_epoch=counter_epoch,
            last_trusted_raw_dc_kwh=trusted_raw_dc_kwh,
            last_changed_at=at,
        ),
        reconcile_state=ReconcileState("required", "provisioned"),
    )


def reprovision(
    state: StoreState,
    *,
    counter_id: str,
    counter_epoch: str,
    trusted_raw_dc_kwh: float,
    at: datetime,
) -> StoreState:
    """Explicit procedure out of a counter change or a lost state: trust a
    (new) counter from now on. Only with nothing in flight — no active grant,
    no open episode, no pending step — so no budget can be re-released; the
    closed ledger stays. Reconcile is required again; the old and new counter
    identity go to the audit."""
    if state.provisioning_state == "uninitialized":
        raise GrantTransitionError("not provisioned yet — use provision")
    if state.active_grant is not None or state.pending_step is not None:
        raise GrantTransitionError("cannot reprovision with a grant or step in flight")
    if any(e.state == "open" for e in state.episode_ledger):
        raise GrantTransitionError("cannot reprovision with an open episode")
    old = state.counter_state
    revision = state.state_revision + 1
    audit = _audit(state, f"reprovision:{revision}", {
        "kind": "reprovisioned",
        "at": int(at.timestamp()),
        "previous_provisioning": state.provisioning_state,
        "previous_reconcile": state.reconcile_state.reason_code,
        "previous_counter_id": None if old is None else old.counter_id,
        "previous_counter_epoch": None if old is None else old.counter_epoch,
        "previous_trusted_raw_dc_kwh": None if old is None else old.last_trusted_raw_dc_kwh,
        "counter_id": counter_id,
        "counter_epoch": counter_epoch,
        "trusted_raw_dc_kwh": trusted_raw_dc_kwh,
    })
    return _bump(
        state,
        provisioning_state="ready",
        counter_state=CounterState(
            counter_id=counter_id,
            counter_epoch=counter_epoch,
            last_trusted_raw_dc_kwh=trusted_raw_dc_kwh,
            last_changed_at=at,
        ),
        reconcile_state=ReconcileState("required", "reprovisioned"),
        pending_audit=audit,
    )


def ack_audit(state: StoreState, event_ids: set[str]) -> StoreState:
    """Drop events only after the outbox durably took them over."""
    remaining = tuple(e for e in state.pending_audit if e.event_id not in event_ids)
    if remaining == state.pending_audit:
        return state
    return _bump(state, pending_audit=remaining)


def prepare_grant(
    state: StoreState,
    *,
    grant: Grant,
    episode_id: str,
    counter_epoch: str,
    now: datetime,
    policy_version: str,
    deadline_at: datetime | None = None,
    plan_ref: str | None = None,
) -> StoreState:
    """New episode → `prepared`. Identity, checkpoint and the open ledger entry
    are part of the returned state, which must be persisted before any
    charge is enabled."""
    if state.provisioning_state != "ready":
        raise GrantTransitionError(f"provisioning is {state.provisioning_state}, not ready")
    if state.active_grant is not None:
        raise GrantTransitionError("a grant is already active")
    if any(e.state == "open" for e in state.episode_ledger):
        raise GrantTransitionError("previous episode is still open")
    if any(e.episode_id == episode_id for e in state.episode_ledger):
        raise GrantTransitionError(f"episode '{episode_id}' already exists (replay)")
    if any(e.original_grant_id == grant.identity.id for e in state.episode_ledger):
        raise GrantTransitionError(f"grant '{grant.identity.id}' already used (replay)")
    if (
        grant.identity.counter_id != state.counter_state.counter_id
        or counter_epoch != state.counter_state.counter_epoch
    ):
        raise GrantTransitionError("grant counter does not match the trusted counter state")
    if grant.window.end <= grant.window.start:
        raise GrantTransitionError("empty grant window — nothing to prepare")

    identity = grant.identity
    active = ActiveGrant(
        identity=ActiveGrantIdentity(
            grant_id=identity.id,
            episode_id=episode_id,
            purpose=identity.purpose,
            created_at=identity.created_at,
            target_pct=identity.target_pct,
            budget_dc_kwh=identity.budget_dc_kwh,
            counter_id=identity.counter_id,
            counter_epoch=counter_epoch,
            checkpoint_dc_kwh=identity.checkpoint_dc_kwh,
        ),
        window=GrantWindow(start=grant.window.start, end=grant.window.end),
        status="prepared",
        reason_code=None,
        status_changed_at=now,
        usage=GrantUsage(
            used_dc_kwh=0.0,
            counter_changed_at=None,
            last_trusted_raw_dc_kwh=identity.checkpoint_dc_kwh,
        ),
        created_policy_version=policy_version,
        plan_ref=plan_ref,
    )
    entry = EpisodeLedgerEntry(
        episode_id=episode_id,
        purpose=identity.purpose,
        state="open",
        original_grant_id=identity.id,
        original_budget_dc_kwh=identity.budget_dc_kwh,
        deadline_at=deadline_at,
    )
    audit = _audit(
        state,
        f"{identity.id}:prepared",
        {"kind": "grant_prepared", "grant_id": identity.id, "episode_id": episode_id},
    )
    return _bump(
        state,
        active_grant=active,
        episode_ledger=(*state.episode_ledger, entry),
        pending_audit=audit,
    )


def step_grant(
    state: StoreState,
    *,
    now: datetime,
    soc_now_pct: float,
    reading: CounterReading,
    reading_epoch: str,
    telemetry_valid: bool,
    is_nt: Callable[[datetime], bool],
    deye_prepared: bool,
    safety_revision: int,
    observed_before: Mapping[str, Any] | None = None,
    budget_factor: float = DEFAULT_GRANT_BUDGET_FACTOR,
    max_charge_rate_kw: float = DEFAULT_MAX_CHARGE_RATE_KW,
) -> StepResult:
    """One evaluation cycle. Enabling requires provisioning `ready`, reconcile
    `ready` and confirmed inverter preparation, and only PROPOSES a step
    (`StepResult.step_id`); the grant becomes `active` after
    `record_step_result` confirms it. Anything uncertain pauses."""
    active = state.active_grant
    if active is None:
        return StepResult(state, False, "no_grant")
    if active.status == "closing":
        return StepResult(state, False, active.reason_code or "closing")
    pending_step = state.pending_step
    step_id: str | None = None

    if reading_epoch != active.identity.counter_epoch:
        # Counter reset/replacement or unproven continuity: keep checkpoint and
        # usage untouched, block until resolved by an explicit procedure.
        paused = _with_status(active, "paused" if active.status == "active" else active.status,
                              "counter_epoch_changed", now)
        candidate = replace(
            state,
            active_grant=paused,
            reconcile_state=ReconcileState("blocked", "counter_epoch_changed"),
        )
        return StepResult(_commit(state, candidate), False, "counter_epoch_changed")

    core = _core_grant(active)
    usage, counter_state, update = _apply_reading(state, active, reading, max_charge_rate_kw)
    decision = evaluate_grant(
        core,
        update.used_dc_kwh,
        now=now,
        soc_now_pct=soc_now_pct,
        telemetry_valid=telemetry_valid,
        is_nt=is_nt,
        counter_trusted=update.trusted,
        counter_distrust_reason=update.reason,
        budget_factor=budget_factor,
    )
    working = replace(active, usage=usage)
    audit = state.pending_audit

    if decision.close:
        closing = _start_closing(replace(state, active_grant=working), decision.reason, now)
        working, audit = closing.active_grant, closing.pending_audit
        charge, reason = False, decision.reason
    elif decision.charge_enable:
        blocker = _enable_blocker(state, deye_prepared)
        if blocker is not None:
            charge, reason = False, blocker
            if working.status == "active":
                working = _with_status(working, "paused", reason, now)
        elif working.status == "active":
            charge, reason = True, None  # already confirmed on, no new write
        elif pending_step is not None:
            # An earlier enable has no recorded outcome: never resend blindly.
            charge, reason = False, "pending_step_unresolved"
        else:
            # Proposal only: stays prepared/paused until the write is confirmed.
            pending_step = PendingStep(
                step_id=f"{active.identity.grant_id}:enable:{state.state_revision + 1}",
                grant_id=active.identity.grant_id,
                episode_id=active.identity.episode_id,
                observed_before=dict(observed_before or {}),
                requested_changes={"charge_enable": True},
                operation_order=("charge_enable",),
                safety_revision=safety_revision,
                operation_status={},
            )
            charge, reason, step_id = True, None, pending_step.step_id
    else:
        # Stopping never waits for confirmation.
        charge, reason = False, decision.reason
        if working.status == "active":
            working = _with_status(working, "paused", reason, now)

    candidate = replace(
        state,
        active_grant=working,
        counter_state=counter_state,
        pending_audit=audit,
        pending_step=pending_step,
    )
    return StepResult(_commit(state, candidate), charge, reason, step_id)


def _enable_blocker(state: StoreState, deye_prepared: bool) -> str | None:
    """Conditions that must ALL hold before anything may enable charging;
    provisioning and reconcile are independent of each other."""
    if state.provisioning_state != "ready":
        return f"provisioning_{state.provisioning_state}"
    if state.reconcile_state.value != "ready":
        return "reconcile_not_ready"
    if not deye_prepared:
        return "deye_not_prepared"
    return None


def record_step_result(
    state: StoreState, *, step_id: str, operation: str, outcome: OperationStatus, now: datetime
) -> StoreState:
    """Record the outcome of one operation of the pending step. Only a
    confirmed readback of every operation makes the grant `active`; `failed`
    means definitely not applied; `unknown` keeps the step and requires
    reconcile — it is never treated as applied."""
    step = state.pending_step
    if step is None or step.step_id != step_id:
        raise GrantTransitionError(f"no pending step '{step_id}'")
    if operation not in step.operation_order:
        raise GrantTransitionError(f"step '{step_id}' has no operation '{operation}'")
    statuses = {**step.operation_status, operation: outcome}
    active = state.active_grant
    open_grant = active is not None and active.status != "closing"

    if outcome == "failed":
        new_active = (
            _with_status(active, "paused", f"{operation}_failed", now) if open_grant else active
        )
        return _bump(state, pending_step=None, active_grant=new_active)
    if outcome == "unknown":
        return _bump(
            state,
            pending_step=replace(step, operation_status=statuses),
            reconcile_state=ReconcileState("required", "step_result_unknown"),
        )
    if outcome == "sent" or not all(statuses.get(op) == "confirmed" for op in step.operation_order):
        return _commit(state, replace(state, pending_step=replace(step, operation_status=statuses)))

    new_active = active
    if open_grant and step.requested_changes.get("charge_enable") is True:
        new_active = _with_status(active, "active", None, now)
    audit = _audit(
        state,
        f"{step_id}:confirmed",
        {"kind": "step_confirmed", "step_id": step_id, "grant_id": step.grant_id},
    )
    return _bump(state, pending_step=None, active_grant=new_active, pending_audit=audit)


def confirm_closed(state: StoreState, *, now: datetime) -> StoreState:
    """`closing` → closed episode + `active_grant=None` + audit event, all in ONE
    returned state — closure, final usage and history never diverge."""
    active = state.active_grant
    if active is None or active.status != "closing":
        raise GrantTransitionError("only a closing grant can be confirmed closed")
    if state.pending_step is not None:
        raise GrantTransitionError("unresolved pending step — physical state unknown")
    episode_id = active.identity.episode_id
    ledger = tuple(
        replace(
            e,
            state="closed",
            final_used_dc_kwh=active.usage.used_dc_kwh,
            close_reason=active.reason_code,
            close_at=now,
        )
        if e.episode_id == episode_id
        else e
        for e in state.episode_ledger
    )
    audit = _audit(
        state,
        f"{active.identity.grant_id}:closed",
        {
            "kind": "grant_closed",
            "grant_id": active.identity.grant_id,
            "episode_id": episode_id,
            "reason": active.reason_code,
            "budget_dc_kwh": active.identity.budget_dc_kwh,
            "final_used_dc_kwh": active.usage.used_dc_kwh,
        },
    )
    return _bump(state, active_grant=None, episode_ledger=ledger, pending_audit=audit)


@dataclass(frozen=True)
class ReconcileResult:
    """`reason` is None when reconciled `ready`. `require_charge_off` asks the
    caller to command charging off (also when the readback is unavailable)."""

    state: StoreState
    require_charge_off: bool
    reason: str | None


def reconcile(
    state: StoreState,
    *,
    now: datetime,
    observed_charge_enable: bool | None,
    reading: CounterReading | None,
    reading_epoch: str | None,
    budget_factor: float = DEFAULT_GRANT_BUDGET_FACTOR,
    max_charge_rate_kw: float = DEFAULT_MAX_CHARGE_RATE_KW,
) -> ReconcileResult:
    """Compare the persisted state with a fresh read-only snapshot (actual
    charge enable setting + charge counter), update usage through the original
    checkpoint, resolve a pending step from the readback, and only then mark
    reconcile `ready`. Never authorizes charging itself."""
    on = observed_charge_enable

    def finish(candidate: StoreState, value: ReconcileValue, reason: str | None,
               off: bool) -> ReconcileResult:
        target = ReconcileState(value, None if value == "ready" else reason)
        if candidate.reconcile_state != target:
            candidate = replace(candidate, reconcile_state=target)
        return ReconcileResult(_commit(state, candidate), off, target.reason_code)

    if state.reconcile_state.value == "blocked":
        return ReconcileResult(state, on is not False, state.reconcile_state.reason_code)
    if on is None:
        return finish(state, "required", "readback_unavailable", True)
    if state.provisioning_state != "ready" or state.counter_state is None:
        return finish(state, "required", "not_provisioned", on)
    if reading is None or reading_epoch is None or not reading.available:
        return finish(state, "required", "counter_unavailable", on)

    counter = state.counter_state
    active = state.active_grant
    if reading_epoch != counter.counter_epoch or reading.counter_id != counter.counter_id:
        return finish(_paused(state, now, "counter_epoch_changed"), "blocked",
                      "counter_epoch_changed", on)

    if active is not None:
        usage, counter_state, update = _apply_reading(state, active, reading, max_charge_rate_kw)
        if not update.trusted:
            # decrease, implausible jump, … = continuity not proven
            return finish(_paused(state, now, update.reason), "blocked", update.reason, on)
        working = replace(state, active_grant=replace(active, usage=usage),
                          counter_state=counter_state)
        # Usage measured during downtime may already exhaust the budget backup:
        # apply that documented stop before anything can be reported ready.
        if active.status != "closing" and grant_budget_exhausted(
            _core_grant(active), usage.used_dc_kwh, budget_factor
        ):
            working = _start_closing(working, "budget_exhausted", now)
    else:
        if (
            counter.last_trusted_raw_dc_kwh is not None
            and reading.raw_kwh < counter.last_trusted_raw_dc_kwh - COUNTER_STEP_TOLERANCE_KWH
        ):
            return finish(state, "blocked", "counter_decreased", on)
        working = state
        if counter.last_trusted_raw_dc_kwh is None or (
            reading.raw_kwh > counter.last_trusted_raw_dc_kwh + COUNTER_STEP_TOLERANCE_KWH
        ):
            working = replace(state, counter_state=replace(
                counter, last_trusted_raw_dc_kwh=reading.raw_kwh, last_changed_at=reading.at))

    step = working.pending_step
    if step is not None:
        if step.operation_order != ("charge_enable",):
            return finish(working, "required", "pending_step_needs_resolution", on)
        working = record_step_result(
            working,
            step_id=step.step_id,
            operation="charge_enable",
            outcome="confirmed" if on else "failed",
            now=now,
        )

    active = working.active_grant
    if active is None:
        if on:
            return finish(working, "required", "charge_without_grant", True)
    elif active.status == "closing":
        if on:
            return finish(working, "required", "closing_stop_pending", True)
        working = confirm_closed(working, now=now)
    elif active.status == "active":
        if not on:
            working = _paused(working, now, "readback_off")
    elif on:  # prepared/paused but physically charging
        return finish(working, "required", "unexpected_charge", True)
    return finish(working, "ready", None, False)


def _start_closing(state: StoreState, reason: str | None, now: datetime) -> StoreState:
    """Persist the stop intent: status `closing` plus a stable audit event."""
    active = state.active_grant
    assert active is not None
    audit = _audit(
        state,
        f"{active.identity.grant_id}:closing",
        {
            "kind": "grant_closing",
            "grant_id": active.identity.grant_id,
            "reason": reason,
            "used_dc_kwh": active.usage.used_dc_kwh,
        },
    )
    return replace(
        state, active_grant=_with_status(active, "closing", reason, now), pending_audit=audit
    )


def _paused(state: StoreState, now: datetime, reason: str | None) -> StoreState:
    active = state.active_grant
    if active is None or active.status != "active":
        return state
    return replace(state, active_grant=_with_status(active, "paused", reason, now))


def _apply_reading(
    state: StoreState, active: ActiveGrant, reading: CounterReading, max_charge_rate_kw: float
) -> tuple[GrantUsage, CounterState | None, Any]:
    update = update_grant_used(
        _core_grant(active),
        reading,
        active.usage.used_dc_kwh,
        active.usage.counter_changed_at,
        max_charge_rate_kw=max_charge_rate_kw,
    )
    usage = GrantUsage(
        used_dc_kwh=update.used_dc_kwh,
        counter_changed_at=update.at,
        last_trusted_raw_dc_kwh=reading.raw_kwh
        if update.trusted
        else active.usage.last_trusted_raw_dc_kwh,
    )
    counter_state = state.counter_state
    if update.trusted and counter_state is not None:
        counter_state = replace(
            counter_state, last_trusted_raw_dc_kwh=reading.raw_kwh, last_changed_at=update.at
        )
    return usage, counter_state, update


def _with_status(
    active: ActiveGrant, status: str, reason: str | None, now: datetime
) -> ActiveGrant:
    if active.status == status and active.reason_code == reason:
        return active
    return replace(active, status=status, reason_code=reason, status_changed_at=now)


def _commit(old: StoreState, candidate: StoreState) -> StoreState:
    """Bump the revision only when something actually changed — a no-op poll
    must not churn the stored state."""
    if candidate == old:
        return old
    return replace(candidate, state_revision=old.state_revision + 1)
