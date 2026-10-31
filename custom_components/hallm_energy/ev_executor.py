"""The writer for the wallbox: carries out the plan of the EV report, step by step.

Writes happen only when the EV gate is live: `manage_ev` on, the shadow switch off, all four
wallbox entities mapped and the phase guard configured. Otherwise the plan is only reported.

A step is a write that is confirmed by reading the value back. Rules:
* protective steps (pausing) go first and are always allowed; they also preempt a sequence that
  is in flight: the sequence checks an epoch before every write and stops;
* before every other step the plan is evaluated again and the step must still be part of it; a
  different state or a missing step stops the sequence;
* the first step that is not confirmed stops the sequence, so nothing after a failed preparation
  (in particular the un-pause) is ever written;
* a step whose value is already in place is skipped;
* the start gate is taken for the whole non-protective sequence, and just before every such step
  the writer checks again that it is still allowed and that the house battery is not charging
  from the grid: nothing that was true at the start of the sequence is trusted at its end;
* handing the battery back (`restore`) needs the wallbox to read paused right then: nothing but
  a confirmed pause says the car draws nothing;
* nothing is enabled while the house battery is charged from the grid (a grant running or being
  prepared): the two loads would add up on the main breaker, and the phase guard is the last line
  of defence, not the plan;
* after a write that failed or was not confirmed, the next attempt (other than a protective one)
  waits `RETRY_AFTER`, so a device that does not answer is not hammered every second;
* routine charging does not restart within `RESTART_HOLD` of a pause and not more than
  `MAX_UNPAUSES_PER_HOUR` times an hour; a forced charge is the owner's wish and is not held.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import EnergyConfig
from .device_io import DeviceIO, WriteResult
from .executor import Gates
from .slot_plan import Op
from .start_gate import StartGate

_LOGGER = logging.getLogger(__name__)

RESTART_HOLD = timedelta(minutes=10)
RETRY_AFTER = timedelta(seconds=30)  # after a write that failed, before the next attempt
MAX_UNPAUSES_PER_HOUR = 3


@dataclass
class EvWriteReport:
    live: bool = False
    result: str = "idle"
    reason: str | None = None
    writes: list[WriteResult] = field(default_factory=list)


class EvExecutor:
    def __init__(self, io: DeviceIO, config: EnergyConfig, gates: Gates,
                 grid_busy: Callable[[], bool] | None = None,
                 start_gate: StartGate | None = None,
                 audit: Callable[[list[dict]], None] | None = None) -> None:
        self._audit = audit
        self.io, self.config, self.gates = io, config, gates
        self.start_gate = start_gate or StartGate()
        self._grid_busy = grid_busy or (lambda: False)
        self._lock = asyncio.Lock()
        self._epoch = 0
        self._last_pause_at: datetime | None = None
        self._unpauses: list[datetime] = []
        self._retry_at: datetime | None = None
        self.last = EvWriteReport()

    def live(self) -> bool:
        cfg = self.config
        return bool(
            cfg.manage_ev
            and self.gates.floors_live()
            and cfg.phase_current_entities
            and all(self._entity(t) for t in ("pause", "intensity", "max_intensity", "dynamic"))
        )

    def _entity(self, target: str) -> str | None:
        cfg = self.config
        return {
            "pause": cfg.ev_wallbox_pause_entity,
            "intensity": cfg.ev_wallbox_intensity_entity,
            "max_intensity": cfg.ev_wallbox_max_intensity_entity,
            "dynamic": cfg.ev_wallbox_dynamic_entity,
            "deye_mode": cfg.ev_deye_mode_entity,
        }.get(target)

    def _held(self, report: dict, now: datetime) -> str | None:
        """Why an un-pause of routine charging has to wait; a forced charge is never held."""
        if report.get("state") == "force_charge":
            return None
        if self._last_pause_at is not None and now - self._last_pause_at < RESTART_HOLD:
            return "restart hold after a pause"
        recent = [t for t in self._unpauses if now - t < timedelta(hours=1)]
        if len(recent) >= MAX_UNPAUSES_PER_HOUR:
            return "too many restarts in the last hour"
        return None

    async def async_apply(
        self,
        report: dict,
        *,
        now: datetime,
        recheck: Callable[[], dict | None],
    ) -> EvWriteReport:
        intents = report.get("intents") or []
        out = EvWriteReport(live=self.live())
        self.last = out
        if not intents:
            out.result = "nothing_to_do"
            return out
        if not out.live:
            out.result = "shadow"
            return out
        # pausing and handing the battery back are steps in the safe direction
        protective = intents[0].get("phase") in ("protect", "restore")
        if protective:
            self._epoch += 1  # whatever is in flight stops at its next check
        elif self._lock.locked():
            out.result = "busy"
            return out
        elif self._grid_busy():
            out.result, out.reason = "held", "grid charging of the house battery in progress"
            return out
        elif self._retry_at is not None and now < self._retry_at:
            out.result, out.reason = "backoff", "waiting after a failed write"
            return out
        gate_taken = False
        if not protective:
            gate_taken = self.start_gate.try_acquire()
            if not gate_taken:
                out.result = "held"
                out.reason = "the house battery is starting to charge from the grid"
                return out
        try:
            return await self._run(report, intents, out, now, recheck, protective)
        finally:
            if gate_taken:
                self.start_gate.release()
            self._record(report, out)

    def _record(self, report: dict, out: EvWriteReport) -> None:
        """What was written, and why a sequence stopped, for the audit (after the sequence, so
        recording never holds a write up). Held and idle outcomes are not events."""
        if self._audit is None:
            return
        targets = {self._entity(t): t for t in ("pause", "intensity", "max_intensity", "dynamic",
                                                "deye_mode")}
        events = [{"kind": "ev_write", "target": targets.get(w.op.entity_id),
                   "entity_id": w.op.entity_id, "value": w.op.value, "outcome": w.outcome,
                   "detail": w.detail, "ev_state": report.get("state")} for w in out.writes]
        if out.result in ("aborted", "preempted"):
            events.append({"kind": "ev_sequence", "result": out.result, "reason": out.reason,
                           "ev_state": report.get("state")})
        if events:
            self._audit(events)

    async def _run(self, report: dict, intents: list, out: EvWriteReport, now: datetime,
                   recheck: Callable[[], dict | None], protective: bool) -> EvWriteReport:
        async with self._lock:
            epoch = self._epoch
            for step in intents:
                if self._epoch != epoch:
                    out.result, out.reason = "preempted", "a protective step took over"
                    return out
                entity = self._entity(step["target"])
                value = step["value"]
                if entity is None:
                    out.result, out.reason = "aborted", f"{step['target']} is not mapped"
                    return out
                if self.io.read(entity) == value:
                    continue
                if step["phase"] == "restore":
                    pause_entity = self._entity("pause")
                    if pause_entity is None or self.io.read(pause_entity) is not True:
                        out.result, out.reason = "aborted", "the wallbox is not confirmed paused"
                        return out
                if step["phase"] not in ("protect", "restore"):
                    if not self.live():
                        out.result, out.reason = "aborted", "the gate closed"
                        return out
                    if self._grid_busy():
                        out.result, out.reason = "held", "grid charging of the house battery began"
                        return out
                    fresh = recheck()
                    still = fresh is not None and fresh.get("state") == report.get("state") and any(
                        s["target"] == step["target"] and s["value"] == value
                        for s in fresh.get("intents") or [])
                    if not still:
                        out.result, out.reason = "aborted", "the plan changed"
                        return out
                if step["phase"] == "enable":
                    held = self._held(report, now)
                    if held is not None:
                        out.result, out.reason = "held", held
                        return out
                write = await self.io.write(Op(entity, value))
                out.writes.append(write)
                _LOGGER.info("EV write %s=%s: %s", entity, value, write.outcome)
                if write.outcome != "confirmed":
                    out.result, out.reason = "aborted", f"{step['target']} {write.outcome}"
                    self._retry_at = now + RETRY_AFTER
                    return out
                self._retry_at = None
                if step["target"] == "pause" and value is True:
                    self._last_pause_at = now
                if step["target"] == "pause" and value is False:
                    self._unpauses.append(now)
                    self._unpauses = [t for t in self._unpauses if now - t < timedelta(hours=1)]
            out.result = "done"
            return out
