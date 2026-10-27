"""Entity mapping chosen in the config flow (stored in the entry options).

Nothing is hardcoded: every entity the integration reads or writes comes from
here. An entry without options (or with an incomplete mapping) is not
configured — the integration then only keeps its state and does nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .recovery_flow import RecoveryParams
from .slot_plan import SlotEntities

SLOT_COUNT = 6

CONF_SOC = "soc_entity"
CONF_COUNTER = "counter_entity"
CONF_COUNTER_EPOCH = "counter_epoch"
CONF_HDO_RELAY = "hdo_relay_entity"
CONF_HDO_SCHEDULE = "hdo_schedule_entity"
CONF_GRID_CHARGE = "grid_charge_entity"
CONF_SHADOW = "shadow_entity"
CONF_FEATURE = "feature_entity"

# Recovery inputs and parameters (optional; defaults below).
CONF_PV_FORECAST = "pv_forecast_entity"
CONF_PROFILE_TOPIC = "house_load_profile_topic"
CONF_TOPOLOGY_EPOCH = "topology_epoch"
CONF_PV_CORRECTION = "pv_correction"
CONF_RESERVE = "reserve_pct"
CONF_RECOVERY_TARGET = "recovery_target_pct"
CONF_RECOVERY_WAIT = "recovery_pv_wait_h"
CONF_BATTERY_KWH = "battery_capacity_kwh"
CONF_ETA = "battery_efficiency"
CONF_E_ERR_MORNING = "error_margin_morning_kwh"
CONF_CHARGE_RATE = "grid_charge_rate_dc_kw"
CONF_BUDGET_FACTOR = "budget_factor"

# name: (default, minimum, maximum)
NUMBER_OPTIONS: dict[str, tuple[float, float, float]] = {
    CONF_RESERVE: (20.0, 10.0, 40.0),
    CONF_RECOVERY_TARGET: (25.0, 15.0, 60.0),
    CONF_RECOVERY_WAIT: (36.0, 0.0, 72.0),
    CONF_PV_CORRECTION: (0.75, 0.3, 1.2),
    CONF_BATTERY_KWH: (46.08, 1.0, 200.0),
    CONF_ETA: (0.95, 0.5, 1.0),
    CONF_E_ERR_MORNING: (2.0, 0.0, 10.0),
    CONF_CHARGE_RATE: (1.0, 0.1, 20.0),
    CONF_BUDGET_FACTOR: (1.25, 1.1, 1.5),
    CONF_TOPOLOGY_EPOCH: (1, 1, 99),
}


def slot_key(kind: str, index: int) -> str:
    """`kind` is `start`, `capacity` or `charge_enable`; `index` is 1-based."""
    return f"slot{index}_{kind}_entity"


SLOT_KINDS = ("start", "capacity", "charge_enable")
SINGLE_KEYS = (
    CONF_SOC,
    CONF_COUNTER,
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
    hdo_relay_entity: str
    hdo_schedule_entity: str
    grid_charge_entity: str
    shadow_entity: str
    feature_entity: str
    slot_start_entities: tuple[str, ...]
    slot_capacity_entities: tuple[str, ...]
    slot_charge_enable_entities: tuple[str, ...]
    pv_forecast_entity: str | None = None
    profile_topic: str | None = None
    pv_correction: float = NUMBER_OPTIONS[CONF_PV_CORRECTION][0]
    budget_factor: float = NUMBER_OPTIONS[CONF_BUDGET_FACTOR][0]
    recovery: RecoveryParams | None = None

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

        numbers = {}
        for key, (default, low, high) in NUMBER_OPTIONS.items():
            value = options.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int | float) \
                    or not low <= value <= high:
                raise ConfigInvalidError(f"{key}: expected a number in [{low}, {high}]")
            numbers[key] = float(value)
        if numbers[CONF_RECOVERY_TARGET] < numbers[CONF_RESERVE] + 5:
            raise ConfigInvalidError("recovery target must be at least reserve + 5")
        topic = options.get(CONF_PROFILE_TOPIC) or None
        if topic is not None and (not isinstance(topic, str) or "#" in topic or "+" in topic):
            raise ConfigInvalidError("house load profile topic must be a plain MQTT topic")
        pv_entity = options.get(CONF_PV_FORECAST) or None

        return EnergyConfig(
            soc_entity=options[CONF_SOC],
            counter_entity=options[CONF_COUNTER],
            counter_epoch=epoch.strip(),
            hdo_relay_entity=options[CONF_HDO_RELAY],
            hdo_schedule_entity=options[CONF_HDO_SCHEDULE],
            grid_charge_entity=options[CONF_GRID_CHARGE],
            shadow_entity=options[CONF_SHADOW],
            feature_entity=options[CONF_FEATURE],
            slot_start_entities=slots("start"),
            slot_capacity_entities=slots("capacity"),
            slot_charge_enable_entities=slots("charge_enable"),
            pv_forecast_entity=pv_entity,
            profile_topic=topic,
            pv_correction=numbers[CONF_PV_CORRECTION],
            budget_factor=numbers[CONF_BUDGET_FACTOR],
            recovery=RecoveryParams(
                reserve_pct=numbers[CONF_RESERVE],
                recovery_target_pct=numbers[CONF_RECOVERY_TARGET],
                recovery_pv_wait_h=numbers[CONF_RECOVERY_WAIT],
                c_use_kwh=numbers[CONF_BATTERY_KWH],
                eta_c=numbers[CONF_ETA],
                eta_d=numbers[CONF_ETA],
                e_err_morning_kwh=numbers[CONF_E_ERR_MORNING],
                assumed_charge_rate_dc_kw=numbers[CONF_CHARGE_RATE],
                topology_epoch=int(numbers[CONF_TOPOLOGY_EPOCH]),
            ),
        )
