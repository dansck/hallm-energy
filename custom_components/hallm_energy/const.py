"""Constants. `DOMAIN` must match `manifest.json` exactly (hassfest checks it)."""

DOMAIN = "hallm_energy"

# Local HA Store instead of an input_text entity (255 chars is not enough for a real grant).
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.state"
