"""Constants for the Nanit integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

DOMAIN = "nanit"
LOGGER = logging.getLogger(__package__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.LIGHT,
    Platform.SELECT,
    Platform.MEDIA_PLAYER,
    Platform.CAMERA,
]

# Read-only mode: only the platforms that observe. No platform here controls
# device settings (camera power/sleep, night light, volume, Sound & Light).
READ_ONLY_PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
]

# Cloud event detection window (seconds)
CLOUD_EVENT_WINDOW = 300

# Cloud event poll interval (seconds)
CLOUD_POLL_INTERVAL = 30

# Network info poll interval (seconds)
NETWORK_POLL_INTERVAL = 300

# Config Keys
CONF_MFA_CODE = "mfa_code"
CONF_MFA_TOKEN = "mfa_token"
# Legacy (dropped in config entry v2.2): referenced only to scrub the key
# from entries created by older versions.
CONF_STORE_CREDENTIALS = "store_credentials"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_BABY_UID = "baby_uid"
CONF_CAMERA_UID = "camera_uid"
CONF_BABY_NAME = "baby_name"
CONF_CAMERA_IP = "camera_ip"
CONF_CAMERA_IPS = "camera_ips"
CONF_SPEAKER_UID = "speaker_uid"
CONF_SPEAKER_IP = "speaker_ip"
CONF_SPEAKER_IPS = "speaker_ips"
CONF_READ_ONLY = "read_only"


def is_read_only(entry: ConfigEntry) -> bool:
    """Return whether the entry is in read-only mode (no device control at all)."""
    return entry.options.get(CONF_READ_ONLY, False) is True


# Default sound list (used when API doesn't return available_sounds)
DEFAULT_SOUND_MACHINE_SOUNDS = (
    "white_noise",
    "birds",
    "waves",
    "wind",
    "rain",
    "water_stream",
    "fan",
    "heartbeat",
    "dryer",
    "vacuum",
)
