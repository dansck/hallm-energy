"""Entity mapping chosen in the config flow (stored in the entry options).

Nothing is hardcoded: every entity the integration reads or writes comes from
here. An entry without options (or with an incomplete mapping) is not
configured — the integration then only keeps its state and does nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .slot_plan import SlotEntities

SLOT_COUNT = 6

CONF_SOC = "soc_entity"
CONF_COUNTER = "counter_entity"
CONF_COUNTER_EPOCH = "counter_epoch"
CONF_LIVENESS = "liveness_entity"
CONF_HDO_RELAY = "hdo_relay_entity"
CONF_HDO_SCHEDULE = "hdo_schedule_entity"
CONF_GRID_CHARGE = "grid_charge_entity"
CONF_SHADOW = "shadow_entity"
CONF_FEATURE = "feature_entity"


def slot_key(kind: str, index: int) -> str:
    """`kind` is `start`, `capacity` or `charge_enable`; `index` is 1-based."""
    return f"slot{index}_{kind}_entity"


SLOT_KINDS = ("start", "capacity", "charge_enable")
SINGLE_KEYS = (
    CONF_SOC,
    CONF_COUNTER,
    CONF_LIVENESS,
    CONF_HDO_RELAY,
    CONF_HDO_SCHEDULE,
    CONF_GRID_CHARGE,
    CONF_SHADOW,
    CONF_FEATURE,
)
SLOT_KEYS = tuple(slot_key(kind, i) for i in range(1, SLOT_COUNT + 1) for kind in SLOT_KINDS)
ENTITY_KEYS = SINGLE_KEYS + SLOT_KEYS


class ConfigInvalidError(ValueError):
    pass


@dataclass(frozen=True)
class EnergyConfig:
    soc_entity: str
    counter_entity: str
    counter_epoch: str
    liveness_entity: str
    hdo_relay_entity: str
    hdo_schedule_entity: str
    grid_charge_entity: str
    shadow_entity: str
    feature_entity: str
    slot_start_entities: tuple[str, ...]
    slot_capacity_entities: tuple[str, ...]
    slot_charge_enable_entities: tuple[str, ...]

    @property
    def slot_entities(self) -> SlotEntities:
        return SlotEntities(
            grid_charge=self.grid_charge_entity,
            slot_capacity=self.slot_capacity_entities,
            slot_charge_enable=self.slot_charge_enable_entities,
        )

    @staticmethod
    def from_options(options: Mapping[str, Any]) -> EnergyConfig | None:
        """`None` when nothing is configured; `ConfigInvalidError` when a
        mapping exists but is incomplete or inconsistent."""
        if not any(options.get(key) for key in ENTITY_KEYS):
            return None
        missing = [key for key in ENTITY_KEYS if not isinstance(options.get(key), str)
                   or not options[key]]
        epoch = options.get(CONF_COUNTER_EPOCH)
        if not isinstance(epoch, str) or not epoch.strip():
            missing.append(CONF_COUNTER_EPOCH)
        if missing:
            raise ConfigInvalidError(f"missing: {', '.join(missing)}")
        written = [options[CONF_GRID_CHARGE]] + [
            options[slot_key(kind, i)]
            for i in range(1, SLOT_COUNT + 1)
            for kind in ("capacity", "charge_enable")
        ]
        if len(set(written)) != len(written):
            raise ConfigInvalidError("an entity is mapped to more than one written role")
        if options[CONF_SHADOW] == options[CONF_FEATURE]:
            raise ConfigInvalidError("shadow and feature switches must differ")

        def slots(kind: str) -> tuple[str, ...]:
            return tuple(options[slot_key(kind, i)] for i in range(1, SLOT_COUNT + 1))

        return EnergyConfig(
            soc_entity=options[CONF_SOC],
            counter_entity=options[CONF_COUNTER],
            counter_epoch=epoch.strip(),
            liveness_entity=options[CONF_LIVENESS],
            hdo_relay_entity=options[CONF_HDO_RELAY],
            hdo_schedule_entity=options[CONF_HDO_SCHEDULE],
            grid_charge_entity=options[CONF_GRID_CHARGE],
            shadow_entity=options[CONF_SHADOW],
            feature_entity=options[CONF_FEATURE],
            slot_start_entities=slots("start"),
            slot_capacity_entities=slots("capacity"),
            slot_charge_enable_entities=slots("charge_enable"),
        )
