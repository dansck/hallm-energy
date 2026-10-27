"""Shape checks for manifest.json/hacs.json. The hassfest/HACS GitHub Action
(.github/workflows/validate.yml) additionally checks them against the real
HA/HACS rules."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANIFEST = ROOT / "custom_components" / "hallm_energy" / "manifest.json"
HACS_JSON = ROOT / "hacs.json"

REQUIRED_MANIFEST_KEYS = {
    "domain",
    "name",
    "codeowners",
    "config_flow",
    "documentation",
    "issue_tracker",
    "iot_class",
    "requirements",
    "version",
}

VERSION_PATTERN = re.compile(r"^\d{8}\.\d+$")  # YYYYMMDD.poradove_cislo_ve_dni


def test_manifest_is_valid_json_with_required_keys():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    missing = REQUIRED_MANIFEST_KEYS - data.keys()
    assert not missing, f"manifest.json: chybi pole {missing}"


def test_manifest_domain_matches_const():
    from custom_components.hallm_energy.const import DOMAIN

    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["domain"] == DOMAIN == "hallm_energy"


def test_manifest_domain_is_valid_ha_identifier():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    domain = data["domain"]
    assert domain == domain.lower()
    assert "-" not in domain, "HA domain nesmi obsahovat pomlcku (musi byt platny identifikator)"
    assert domain.isidentifier()


def test_hacs_json_is_valid_json_with_name():
    data = json.loads(HACS_JSON.read_text(encoding="utf-8"))
    assert data.get("name")


def test_manifest_version_follows_date_sequence_scheme():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = data["version"]
    assert VERSION_PATTERN.match(version), (
        f"version '{version}' neodpovida schematu YYYYMMDD.poradove_cislo_ve_dni"
    )


def test_manifest_keys_are_hassfest_sorted():
    # hassfest: domain, name, pak zbytek abecedne
    raw = MANIFEST.read_text(encoding="utf-8")
    keys = list(json.loads(raw).keys())
    assert keys[0] == "domain"
    assert keys[1] == "name"
    assert keys[2:] == sorted(keys[2:])
