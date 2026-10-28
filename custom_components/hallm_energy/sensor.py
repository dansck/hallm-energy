"""One diagnostic sensor: overall status plus details of the stored state and
the last tick. Display only — nothing reads it back."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .data import HallmEnergyData


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([StatusSensor(entry)])


class StatusSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._data: HallmEnergyData = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Hallm Energy",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        if self._data.controller is not None:
            self.async_on_remove(self._data.controller.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str:
        runtime, state = self._data.runtime, self._data.runtime.state
        if runtime.blocked_reason is not None or state is None:
            return "blocked"
        if self._data.controller is None:
            return "not_configured"
        if state.reconcile_state.value == "blocked":
            return "blocked"
        if state.provisioning_state != "ready":
            return "not_provisioned"
        if state.reconcile_state.value != "ready":
            return "reconcile_required"
        if state.active_grant is not None:
            return f"grant_{state.active_grant.status}"
        if state.recovery is not None:
            return "recovery_wait_pv"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data, runtime = self._data, self._data.runtime
        state = runtime.state
        attrs: dict[str, Any] = {
            "blocked_reason": runtime.blocked_reason,
            "persistence_fault": runtime.persistence_fault,
            "config_error": data.config_error,
        }
        if state is not None:
            attrs |= {
                "provisioning": state.provisioning_state,
                "reconcile": state.reconcile_state.value,
                "reconcile_reason": state.reconcile_state.reason_code,
                "pending_audit": len(state.pending_audit),
            }
            if state.counter_state is not None:
                attrs["counter_last_trusted_kwh"] = state.counter_state.last_trusted_raw_dc_kwh
            recovery = state.recovery
            if recovery is not None:
                attrs |= {
                    "recovery_state": recovery.state,
                    "recovery_entered_at": recovery.entered_at.isoformat(),
                    "recovery_wait_until": recovery.wait_until.isoformat(),
                    "recovery_reason": recovery.reason_code,
                }
            grant = state.active_grant
            if grant is not None:
                attrs |= {
                    "grant_id": grant.identity.grant_id,
                    "grant_purpose": grant.identity.purpose,
                    "grant_reason": grant.reason_code,
                    "grant_target_pct": grant.identity.target_pct,
                    "grant_budget_dc_kwh": grant.identity.budget_dc_kwh,
                    "grant_used_dc_kwh": grant.usage.used_dc_kwh,
                    "grant_window_end": grant.window.end.isoformat(),
                }
        controller = data.controller
        if controller is not None:
            tick = controller.status
            attrs |= {
                "live": tick.live,
                "last_tick": None if tick.at is None else tick.at.isoformat(),
                "last_result": tick.result,
                "last_reason": tick.reason,
                "problems": list(tick.problems),
                "intents": [f"{op.entity_id}={op.value}" for op in tick.intents],
                "writes": [f"{w.op.entity_id}={w.op.value}:{w.outcome}" for w in tick.writes],
                "last_error": tick.error,
                "morning_phase": None if tick.morning is None else tick.morning.phase,
                "morning_reason": None if tick.morning is None else tick.morning.reason,
                "t_pv_start": None if tick.morning is None or tick.morning.t_pv_start is None
                else tick.morning.t_pv_start.isoformat(),
                "t_pv_end": None if tick.morning is None or tick.morning.t_pv_end is None
                else tick.morning.t_pv_end.isoformat(),
                "floors": tick.floors,
                "recovery_action": None if tick.recovery is None else tick.recovery.action,
                "recovery_action_reason": None if tick.recovery is None
                else tick.recovery.reason,
                "pv_not_credited_because": None if tick.recovery is None
                else tick.recovery.pv_blocker,

            }
            profile = controller.profiles.profile
            attrs |= {
                "profile_generated_at": None if profile is None
                else profile.generated_at.isoformat(),
                "profile_valid_until": None if profile is None
                else profile.valid_until.isoformat(),
                "profile_quality": None if profile is None else profile.quality.status,
                "profile_error": controller.profiles.error,
                "pv_forecast_retrieved_at": None if controller.pv_forecasts.forecast is None
                else controller.pv_forecasts.forecast.retrieved_at.isoformat(),
                "pv_forecast_valid_until": None if controller.pv_forecasts.forecast is None
                else controller.pv_forecasts.forecast.valid_until.isoformat(),
                "pv_forecast_quality": None if controller.pv_forecasts.forecast is None
                else controller.pv_forecasts.forecast.quality,
                "pv_forecast_error": controller.pv_forecasts.error,
            }
        return attrs
