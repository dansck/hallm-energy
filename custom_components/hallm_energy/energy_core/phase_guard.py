"""Per-phase current guard: a software relief in front of the main breaker.

Reads the worst import and the worst export phase current and, when one stays over
the trip level, steps down what the integration controls: first the EV current cap,
then an EV pause, then grid charging (import branch); the export cap (export branch).
The two branches are evaluated independently. A branch that is over the level never
touches the other one: reducing consumption on an exporting phase would only raise
the export.

The result is a restriction, never an authorisation: `grid_charge_enabled=True` does
not mean charging is allowed, that is always composed with a valid grant and the tariff.
The guard is not a writer; it does not replace the electrical protection of the
installation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

TRIP_A = 21.0
RELEASE_A = 17.0
TRIP_HOLD_S = 10.0
RELEASE_HOLD_S = 300.0
STALE_S = 30.0
EV_CURRENT_STEP_A = 2.0
EV_CURRENT_MIN_A = 6.0
EXPORT_STEP_W = 1000.0
EXPORT_MIN_W = 0.0


@dataclass(frozen=True)
class PhaseGuardState:
    ev_current_cap_a: float
    ev_paused: bool
    grid_charge_enabled: bool
    export_cap_w: float
    import_over_since: datetime | None = None
    import_under_since: datetime | None = None
    export_over_since: datetime | None = None
    export_under_since: datetime | None = None


@dataclass(frozen=True)
class PhaseGuardDecision:
    ev_current_cap_a: float
    ev_paused: bool
    grid_charge_enabled: bool
    export_cap_w: float
    notify: bool
    next_state: PhaseGuardState


def _step_import_branch(
    cap_a: float, paused: bool, grid_on: bool, *, min_a: float, step_a: float
) -> tuple[float, bool, bool]:
    if paused:
        return cap_a, True, False  # next level after the pause: grid charging off
    if cap_a - step_a >= min_a:
        return cap_a - step_a, False, grid_on
    return cap_a, True, grid_on  # next level: pause the EV


def _release_import_branch(
    cap_a: float, paused: bool, grid_on: bool, *, step_a: float, default_a: float, min_a: float
) -> tuple[float, bool, bool]:
    if not grid_on:
        return cap_a, paused, True  # first restore grid charging
    if paused:
        return min_a, False, grid_on  # then un-pause at the minimum
    return min(default_a, cap_a + step_a), False, grid_on


def phase_guard(
    state: PhaseGuardState,
    *,
    now: datetime,
    measured_at: datetime,
    max_import_phase_a: float | None,
    max_export_phase_a: float | None,
    default_export_cap_w: float,
    default_ev_current_cap_a: float,
    trip_a: float = TRIP_A,
    release_a: float = RELEASE_A,
    trip_hold_s: float = TRIP_HOLD_S,
    release_hold_s: float = RELEASE_HOLD_S,
    stale_s: float = STALE_S,
) -> PhaseGuardDecision:
    """One evaluation step.

    `max_import_phase_a` / `max_export_phase_a` are the worst values over all phases for
    the branch (a phase that imports counts for the import branch, one that exports for
    the export branch). Every `trip_hold_s` of continued excess takes one more step; the
    release steps back every `release_hold_s` below `release_a`. With stale or missing
    data the restriction in force is kept, but the timers are dropped: a gap without
    trustworthy data breaks continuity, so it never counts towards a release.
    """
    stale = (now - measured_at).total_seconds() > stale_s
    if stale or max_import_phase_a is None or max_export_phase_a is None:
        frozen = PhaseGuardState(
            state.ev_current_cap_a, state.ev_paused, state.grid_charge_enabled, state.export_cap_w
        )
        return PhaseGuardDecision(
            state.ev_current_cap_a,
            state.ev_paused,
            state.grid_charge_enabled,
            state.export_cap_w,
            False,
            frozen,
        )

    cap_a, paused, grid_on = state.ev_current_cap_a, state.ev_paused, state.grid_charge_enabled
    import_over_since = state.import_over_since
    import_under_since = state.import_under_since
    notify = False

    if max_import_phase_a > trip_a:
        import_under_since = None
        import_over_since = import_over_since or now
        if (now - import_over_since).total_seconds() >= trip_hold_s:
            cap_a, paused, grid_on = _step_import_branch(
                cap_a, paused, grid_on, min_a=EV_CURRENT_MIN_A, step_a=EV_CURRENT_STEP_A
            )
            import_over_since = now
            if paused and not grid_on:
                notify = True
    elif max_import_phase_a < release_a:
        import_over_since = None
        import_under_since = import_under_since or now
        restricted = paused or not grid_on or cap_a < default_ev_current_cap_a
        if restricted and (now - import_under_since).total_seconds() >= release_hold_s:
            cap_a, paused, grid_on = _release_import_branch(
                cap_a,
                paused,
                grid_on,
                step_a=EV_CURRENT_STEP_A,
                default_a=default_ev_current_cap_a,
                min_a=EV_CURRENT_MIN_A,
            )
            import_under_since = now
    else:
        import_over_since = None
        import_under_since = None

    export_cap = state.export_cap_w
    export_over_since = state.export_over_since
    export_under_since = state.export_under_since

    if max_export_phase_a > trip_a:
        export_under_since = None
        export_over_since = export_over_since or now
        if (now - export_over_since).total_seconds() >= trip_hold_s:
            if export_cap - EXPORT_STEP_W >= EXPORT_MIN_W:
                export_cap -= EXPORT_STEP_W
            else:
                export_cap = EXPORT_MIN_W
                notify = True
            export_over_since = now
    elif max_export_phase_a < release_a:
        export_over_since = None
        export_under_since = export_under_since or now
        if export_cap < default_export_cap_w and (
            now - export_under_since
        ).total_seconds() >= release_hold_s:
            export_cap = min(default_export_cap_w, export_cap + EXPORT_STEP_W)
            export_under_since = now
    else:
        export_over_since = None
        export_under_since = None

    next_state = PhaseGuardState(
        cap_a,
        paused,
        grid_on,
        export_cap,
        import_over_since,
        import_under_since,
        export_over_since,
        export_under_since,
    )
    return PhaseGuardDecision(cap_a, paused, grid_on, export_cap, notify, next_state)


def branch_currents(
    currents_a: tuple[float | None, ...], powers_w: tuple[float | None, ...]
) -> tuple[float | None, float | None]:
    """Worst import and worst export phase current from per-phase RMS currents (no sign)
    and per-phase active power (+ import, - export). A phase with power >= 0 counts for
    the import branch, a negative one for the export branch; a branch without any phase
    is 0 A. `None` for both when any phase is missing: nothing is guessed."""
    if len(currents_a) != len(powers_w) or not currents_a:
        return None, None
    worst_import, worst_export = 0.0, 0.0
    for current, power in zip(currents_a, powers_w, strict=True):
        if current is None or power is None:
            return None, None
        if power >= 0:
            worst_import = max(worst_import, current)
        else:
            worst_export = max(worst_export, current)
    return worst_import, worst_export
