from __future__ import annotations

import pytest


@pytest.fixture
def storage_hass(hass, tmp_path):
    """Test hass whose config dir (and so `.storage/`) is a fresh temp dir — the
    state file is written for real, never into the shared test config."""
    hass.config.config_dir = str(tmp_path)
    return hass


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let Home Assistant's loader find `custom_components/hallm_energy`."""
