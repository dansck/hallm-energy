"""What a loaded config entry carries (`entry.runtime_data`)."""

from __future__ import annotations

from dataclasses import dataclass

from .controller import EnergyController
from .runtime import EnergyRuntime


@dataclass
class HallmEnergyData:
    runtime: EnergyRuntime
    controller: EnergyController | None  # None = not configured (or invalid mapping)
    config_error: str | None = None
