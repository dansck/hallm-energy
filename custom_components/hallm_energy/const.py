"""Constants. `DOMAIN` must match `manifest.json` exactly (hassfest checks it)."""

from datetime import timedelta

DOMAIN = "hallm_energy"

# Local HA Store instead of an input_text entity (255 chars is not enough for a real grant).
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.state"

# Identity of the lifecycle rules recorded in the stored state.
POLICY_VERSION = "20261027.1"

# Config entry data key: installation marker kept outside the Store.
CONF_INSTALLATION_ID = "installation_id"

# Control loop period; gate changes trigger an extra tick immediately.
TICK_INTERVAL = timedelta(seconds=30)

SERVICE_PROVISION = "provision"
SERVICE_REPROVISION = "reprovision"
SERVICE_SAFETY_STOP = "safety_stop"
