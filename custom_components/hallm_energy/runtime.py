"""Holds the integration's persisted state at runtime.

Startup (`async_start`) decides between a genuine first run, a lost state and
a normal restart using an installation marker kept OUTSIDE the Store (in the
config entry): Home Assistant moves a corrupt storage file aside and then
reports "nothing saved", so the Store alone cannot tell "never existed" from
"was lost". Unusable stored data is never overwritten — the runtime stays
blocked and keeps it for diagnosis.

`async_commit` is the only way the in-memory state changes: the new state is
written durably first and adopted only after the write is confirmed.
No device writes happen here.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant

from .const import POLICY_VERSION
from .energy_core import CORE_VERSION
from .lifecycle import lost_state, new_installation_state, on_startup
from .state_schema import StoreState, StoreStateError, parse_store_state
from .store import LocalStateStore, StoreLoadError


class RuntimeBlockedError(Exception):
    """The runtime is blocked; it must not write or authorize anything."""


class EnergyRuntime:
    def __init__(self, hass: HomeAssistant, store: LocalStateStore) -> None:
        self.hass = hass
        self.store = store
        self.state: StoreState | None = None
        self.blocked_reason: str | None = None

    async def async_start(
        self, *, known_installation_id: str | None, new_installation_id: Callable[[], str]
    ) -> str | None:
        """Load, validate and reconcile-gate the stored state. Returns the
        installation id the config entry marker should hold."""
        try:
            raw = await self.store.async_load()
        except StoreLoadError:
            self.blocked_reason = "store_unusable"
            return known_installation_id

        if raw is None:
            if known_installation_id is None:
                state = new_installation_state(
                    installation_id=new_installation_id(),
                    policy_version=POLICY_VERSION,
                    core_version=CORE_VERSION,
                )
            else:
                state = lost_state(
                    installation_id=known_installation_id,
                    policy_version=POLICY_VERSION,
                    core_version=CORE_VERSION,
                    reason="store_lost",
                )
            await self.async_commit(state)
            return state.installation_id

        try:
            state = parse_store_state(raw)
        except StoreStateError:
            self.blocked_reason = "store_invalid"
            return known_installation_id
        if known_installation_id is not None and state.installation_id != known_installation_id:
            self.blocked_reason = "installation_mismatch"
            return known_installation_id

        await self.async_commit(on_startup(state))
        return state.installation_id

    async def async_commit(self, new_state: StoreState) -> None:
        if self.blocked_reason is not None:
            raise RuntimeBlockedError(self.blocked_reason)
        await self.store.async_save(new_state.to_dict())
        self.state = new_state

    @property
    def may_enable(self) -> bool:
        """Necessary (not sufficient) condition for any enabling action."""
        return (
            self.blocked_reason is None
            and self.state is not None
            and self.state.provisioning_state == "ready"
            and self.state.reconcile_state.value == "ready"
        )
