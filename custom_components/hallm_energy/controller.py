"""The periodic control loop: one tick at a time.

Each tick:

1. builds the input snapshot from Home Assistant states;
2. leaving live mode (gates just closed) turns charging off once;
3. after a persistence fault, retries a verified write first (reconcile is
   then required again) — nothing enables in between;
4. an `active` grant whose charging was switched off from outside is sent
   back through reconcile (it becomes `paused`, with an audit event);
5. while reconcile is not `ready`, reconciles against the read-only snapshot
   (stopping first if required, persisting second);
6. otherwise runs one executor cycle.

The safety stop service does not take the tick lock — it preempts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .config import EnergyConfig
from .device_io import DeviceIO, WriteResult
from .executor import Gates, GrantExecutor, Snapshot
from .inputs import build_inputs
from .lifecycle import GrantTransitionError, provision, reconcile, reprovision, set_reconcile
from .runtime import EnergyRuntime
from .slot_plan import Op, Readback, stop_ops
from .state_schema import StoreState
from .store import StoreNotDurableError

_LOGGER = logging.getLogger(__name__)


class ProvisionError(Exception):
    pass


@dataclass
class TickStatus:
    """Outcome of the last tick, for diagnostics only."""

    at: datetime | None = None
    live: bool = False
    result: str = "not_run"
    reason: str | None = None
    problems: tuple[str, ...] = ()
    intents: list[Op] = field(default_factory=list)
    writes: list[WriteResult] = field(default_factory=list)
    error: str | None = None


class EnergyController:
    def __init__(
        self,
        hass: HomeAssistant,
        runtime: EnergyRuntime,
        config: EnergyConfig,
        *,
        io: DeviceIO | None = None,
    ) -> None:
        self.hass = hass
        self.runtime = runtime
        self.config = config
        self.io = io or DeviceIO(hass)
        self.gates = Gates(hass, shadow_entity=config.shadow_entity,
                           feature_entity=config.feature_entity)
        self.executor = GrantExecutor(runtime, self.io, config.slot_entities, self.gates)
        self.status = TickStatus()
        self._lock = asyncio.Lock()
        self._was_live = False
        self._listeners: list[Callable[[], None]] = []

    # --- diagnostics listeners -----------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # --- loop ----------------------------------------------------------------------

    async def async_tick(self, now: datetime | None = None) -> TickStatus:
        async with self._lock:
            now = now or dt_util.utcnow()
            inputs = build_inputs(self.hass, self.config, now)
            status = TickStatus(at=now, live=self.gates.live(), problems=inputs.problems)
            try:
                if self._was_live and not status.live:
                    status.writes += await self.executor.async_release()
                self._was_live = status.live
                await self._step(inputs.snapshot, status)
            except Exception as exc:  # the loop must survive; nothing enables after it
                _LOGGER.exception("tick failed")
                status.result, status.error = "error", repr(exc)
            self.status = status
            self._notify()
            return status

    async def async_release(self) -> None:
        """Called on unload: if live, leave charging off behind."""
        async with self._lock:
            if self._was_live:
                await self.executor.async_release()
                self._was_live = False

    async def async_safety_stop(self) -> list[WriteResult]:
        return await self.executor.async_safety_stop()

    async def async_provision(self, *, again: bool = False) -> None:
        """Explicit: trust the configured charge counter from now on. `again` =
        the procedure out of a counter change / lost state (`reprovision`)."""
        async with self._lock:
            state = self.runtime.state
            if self.runtime.blocked_reason is not None or state is None:
                raise ProvisionError(f"runtime blocked: {self.runtime.blocked_reason}")
            if not again and state.provisioning_state != "uninitialized":
                raise ProvisionError(f"provisioning is {state.provisioning_state}")
            now = dt_util.utcnow()
            reading = build_inputs(self.hass, self.config, now).snapshot.reading
            if not reading.available:
                raise ProvisionError("charge counter is unavailable")
            transition = reprovision if again else provision
            try:
                new_state = transition(
                    state,
                    counter_id=reading.counter_id,
                    counter_epoch=self.config.counter_epoch,
                    trusted_raw_dc_kwh=reading.raw_kwh,
                    at=now,
                )
            except GrantTransitionError as exc:
                raise ProvisionError(str(exc)) from exc
            await self.runtime.async_commit(new_state)

    # --- one step ------------------------------------------------------------------

    async def _step(self, snapshot: Snapshot, status: TickStatus) -> None:
        runtime = self.runtime
        if runtime.blocked_reason is not None or runtime.state is None:
            status.result = f"blocked:{runtime.blocked_reason}"
            return
        readback = self.io.read_slots(self.config.slot_entities)
        if runtime.persistence_fault is not None and not await runtime.async_verify_storage():
            status.result = "persistence_fault"
            await self._stop_if_charging(readback, status)
            return

        state = runtime.state
        active = state.active_grant
        if (
            state.reconcile_state.value == "ready"
            and active is not None
            and active.status == "active"
            and state.pending_step is None
            and readback.charging_enabled is False
        ):
            if not await self._commit(set_reconcile(state, "required", "readback_off"), status):
                return

        if runtime.state.reconcile_state.value != "ready":
            await self._reconcile(snapshot, readback, status)
            if runtime.state.reconcile_state.value != "ready":
                return

        report = await self.executor.async_cycle(snapshot)
        status.result = "cycle"
        status.reason = report.reason
        status.intents += report.intents
        status.writes += report.writes
        status.error = report.error

    async def _reconcile(self, snapshot: Snapshot, readback: Readback,
                         status: TickStatus) -> None:
        state = self.runtime.state
        result = reconcile(
            state,
            now=snapshot.now,
            observed_charge_enable=readback.charging_enabled,
            reading=snapshot.reading,
            reading_epoch=snapshot.reading_epoch,
        )
        status.result = f"reconcile:{result.state.reconcile_state.value}"
        status.reason = result.reason
        if result.require_charge_off:
            await self._stop_if_charging(readback, status, force=True)
        if result.state != state:
            await self._commit(result.state, status)

    async def _stop_if_charging(self, readback: Readback, status: TickStatus,
                                *, force: bool = False) -> None:
        if not force and readback.charging_enabled is False:
            return
        if status.live:
            status.writes += await self.executor.async_safety_stop()
        else:
            status.intents += stop_ops(self.config.slot_entities)

    async def _commit(self, new_state: StoreState, status: TickStatus) -> bool:
        try:
            await self.runtime.async_commit(new_state)
        except StoreNotDurableError as exc:
            status.error = f"state not persisted: {exc}"
            return False
        return True
