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
    DEFAULT_GRANT_BUDGET_FACTOR,
    DEFAULT_MAX_CHARGE_RATE_KW,
    CounterReading,
    Grant,
    GrantIdentity,
    evaluate_grant,
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
    always required before normal actions resume."""
    return set_reconcile(state, "required", "startup")


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
    update = update_grant_used(
        core,
        reading,
        active.usage.used_dc_kwh,
        active.usage.counter_changed_at,
        max_charge_rate_kw=max_charge_rate_kw,
    )
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

    usage = GrantUsage(
        used_dc_kwh=update.used_dc_kwh,
        counter_changed_at=update.at,
        last_trusted_raw_dc_kwh=reading.raw_kwh
        if update.trusted
        else active.usage.last_trusted_raw_dc_kwh,
    )
    counter_state = (
        CounterState(
            counter_id=state.counter_state.counter_id,
            counter_epoch=state.counter_state.counter_epoch,
            last_trusted_raw_dc_kwh=reading.raw_kwh,
            last_changed_at=update.at,
        )
        if update.trusted
        else state.counter_state
    )
    working = replace(active, usage=usage)
    audit = state.pending_audit

    if decision.close:
        working = _with_status(working, "closing", decision.reason, now)
        audit = _audit(
            state,
            f"{active.identity.grant_id}:closing",
            {
                "kind": "grant_closing",
                "grant_id": active.identity.grant_id,
                "reason": decision.reason,
                "used_dc_kwh": usage.used_dc_kwh,
            },
        )
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
