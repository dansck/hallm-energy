"""Home Assistant adapter: read entity values and perform one write with an
explicit outcome. A successful service call is NOT a confirmation — only a
readback of the expected value is.

Outcomes:
* `confirmed` — the entity reads the requested value after the write;
* `failed` — the service call was rejected AND the value did not change
  (definitely not applied);
* `unknown` — anything else (call timeout, accepted call without a matching
  readback within the timeout). Never treated as applied or as not applied.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .slot_plan import Op, Readback, SlotEntities, matches

WriteOutcome = Literal["confirmed", "failed", "unknown"]

_BOOL_DOMAINS = {"switch", "input_boolean"}
_NUMBER_DOMAINS = {"number", "input_number"}


@dataclass(frozen=True)
class WriteResult:
    op: Op
    outcome: WriteOutcome
    detail: str | None = None


class DeviceIO:
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        call_timeout_s: float = 10.0,
        readback_timeout_s: float = 30.0,
        poll_interval_s: float = 0.5,
    ) -> None:
        self.hass = hass
        self.call_timeout_s = call_timeout_s
        self.readback_timeout_s = readback_timeout_s
        self.poll_interval_s = poll_interval_s

    def read(self, entity_id: str) -> bool | float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        domain = entity_id.split(".", 1)[0]
        if domain in _BOOL_DOMAINS:
            return {"on": True, "off": False}.get(state.state)
        try:
            return float(state.state)
        except ValueError:
            return None

    def read_slots(self, entities: SlotEntities) -> Readback:
        def as_bool(entity_id: str) -> bool | None:
            value = self.read(entity_id)
            return value if isinstance(value, bool) else None

        def as_float(entity_id: str) -> float | None:
            value = self.read(entity_id)
            return None if value is None or isinstance(value, bool) else value

        return Readback(
            grid_charge=as_bool(entities.grid_charge),
            slot_capacity=tuple(as_float(e) for e in entities.slot_capacity),
            slot_charge_enable=tuple(as_bool(e) for e in entities.slot_charge_enable),
        )

    async def write(self, op: Op) -> WriteResult:
        domain, service, data = _service_for(op)
        before = self.read(op.entity_id)
        call_error: str | None = None
        rejected = False
        try:
            async with asyncio.timeout(self.call_timeout_s):
                await self.hass.services.async_call(
                    domain, service, {"entity_id": op.entity_id, **data}, blocking=True
                )
        except TimeoutError:
            call_error = "service call timed out"
        except (HomeAssistantError, ValueError) as exc:
            call_error = f"service call rejected: {exc}"
            rejected = True

        if await self._wait_for(op):
            return WriteResult(op, "confirmed", call_error)
        if rejected and self.read(op.entity_id) == before:
            return WriteResult(op, "failed", call_error)
        return WriteResult(op, "unknown", call_error or "no matching readback")

    async def _wait_for(self, op: Op) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.readback_timeout_s
        while True:
            if matches(op, self.read(op.entity_id)):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(self.poll_interval_s)


def _service_for(op: Op) -> tuple[str, str, dict[str, float]]:
    domain = op.entity_id.split(".", 1)[0]
    if domain in _BOOL_DOMAINS:
        if not isinstance(op.value, bool):
            raise ValueError(f"{op.entity_id}: switch needs a bool value")
        return domain, "turn_on" if op.value else "turn_off", {}
    if domain in _NUMBER_DOMAINS:
        if isinstance(op.value, bool):
            raise ValueError(f"{op.entity_id}: number needs a numeric value")
        return domain, "set_value", {"value": op.value}
    raise ValueError(f"{op.entity_id}: unsupported domain '{domain}'")
