"""The bundled `energy_core` must be importable on its own (no external package)."""

from __future__ import annotations


def test_bundled_core_imports_without_external_packages():
    from custom_components.hallm_energy.energy_core import CORE_VERSION, grant

    assert CORE_VERSION
    assert callable(grant.evaluate_grant)
