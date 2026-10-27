"""Pure write plans for an inverter with time-of-use slots (no HA, no I/O).

Each slot has a `capacity` (SOC %) that is BOTH the discharge floor and the
grid-charge target, plus a per-slot `charge_enable`; a global `grid_charge`
switch is the master permission. Grid charging happens only with the slot's
`charge_enable` on AND `grid_charge` on.

Order that keeps a crash at any point safe:

* prepare (never enables anything): `grid_charge` off, every slot
  `charge_enable` off, then every slot floor raised to at least the grant
  target — a later partial state can at worst hold the battery higher;
* enable (only in the current slot): that slot's capacity exactly the
  target, then its `charge_enable` on, and `grid_charge` on LAST;
* stop: `grid_charge` off FIRST, then every slot `charge_enable` off. Floors
  stay raised; restoring normal floors is a separate policy decision made
  only after the stop is confirmed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time

DEFAULT_SLOT_STARTS = (time(6), time(7), time(9), time(10), time(16), time(17))
CAPACITY_TOLERANCE_PCT = 0.5


@dataclass(frozen=True)
class SlotEntities:
    grid_charge: str
    slot_capacity: tuple[str, ...]
    slot_charge_enable: tuple[str, ...]
    slot_starts: tuple[time, ...] = DEFAULT_SLOT_STARTS

    def __post_init__(self) -> None:
        n = len(self.slot_starts)
        if n == 0 or len(self.slot_capacity) != n or len(self.slot_charge_enable) != n:
            raise ValueError("slot entities and slot starts must have the same, non-zero length")
        if list(self.slot_starts) != sorted(self.slot_starts):
            raise ValueError("slot starts must be ascending")


@dataclass(frozen=True)
class Op:
    entity_id: str
    value: bool | float


@dataclass(frozen=True)
class Readback:
    """Current values; `None` = unavailable/unknown."""

    grid_charge: bool | None
    slot_capacity: tuple[float | None, ...]
    slot_charge_enable: tuple[bool | None, ...]

    @property
    def complete(self) -> bool:
        return (
            self.grid_charge is not None
            and None not in self.slot_capacity
            and None not in self.slot_charge_enable
        )

    @property
    def charging_enabled(self) -> bool | None:
        """Physically able to grid-charge right now; `None` if unknown."""
        if self.grid_charge is False or (
            None not in self.slot_charge_enable and not any(self.slot_charge_enable)
        ):
            return False
        if self.grid_charge is None or None in self.slot_charge_enable:
            return None
        return True


def current_slot(entities: SlotEntities, local_time: time) -> int:
    """Index of the slot active at `local_time`; before the first start the
    last slot (from the previous day) is still active."""
    index = len(entities.slot_starts) - 1
    for i, start in enumerate(entities.slot_starts):
        if start <= local_time:
            index = i
    return index


def prepare_ops(entities: SlotEntities, target_pct: float, readback: Readback) -> list[Op]:
    ops: list[Op] = [Op(entities.grid_charge, False)]
    ops += [Op(entity, False) for entity in entities.slot_charge_enable]
    for entity, current in zip(entities.slot_capacity, readback.slot_capacity, strict=True):
        if current is None or current < target_pct - CAPACITY_TOLERANCE_PCT:
            ops.append(Op(entity, float(target_pct)))
    return ops


def is_prepared(target_pct: float, readback: Readback) -> bool:
    """Every permission off and every floor at least the target, all known."""
    return (
        readback.complete
        and readback.grid_charge is False
        and not any(readback.slot_charge_enable)
        and all(c >= target_pct - CAPACITY_TOLERANCE_PCT for c in readback.slot_capacity)
    )


def enable_ops(entities: SlotEntities, slot: int, target_pct: float) -> list[Op]:
    return [
        Op(entities.slot_capacity[slot], float(target_pct)),
        Op(entities.slot_charge_enable[slot], True),
        Op(entities.grid_charge, True),
    ]


def stop_ops(entities: SlotEntities) -> list[Op]:
    return [Op(entities.grid_charge, False)] + [
        Op(entity, False) for entity in entities.slot_charge_enable
    ]


def matches(op: Op, value: bool | float | None) -> bool:
    if value is None:
        return False
    if isinstance(op.value, bool):
        return value is op.value
    if isinstance(value, bool):
        return False
    return abs(float(value) - op.value) <= CAPACITY_TOLERANCE_PCT


def floors_at_least(target_pct: float, capacities: Sequence[float | None]) -> bool:
    return all(c is not None and c >= target_pct - CAPACITY_TOLERANCE_PCT for c in capacities)
