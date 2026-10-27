"""Constants. `DOMAIN` must match `manifest.json` exactly (hassfest checks it)."""

DOMAIN = "hallm_energy"

# Local HA Store instead of an input_text entity (255 chars is not enough for a real grant).
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.state"

# Identity of the lifecycle rules recorded in the stored state.
POLICY_VERSION = "20261027.1"

# Config entry data key: installation marker kept outside the Store.
CONF_INSTALLATION_ID = "installation_id"
