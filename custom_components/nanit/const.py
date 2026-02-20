"""Constants for the Nanit integration."""
import logging
from homeassistant.const import Platform

DOMAIN = "nanit"
LOGGER = logging.getLogger(__package__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.CAMERA,
    Platform.EVENT,
]

# Config Keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_MFA_CODE = "mfa_code"
CONF_MFA_TOKEN = "mfa_token"
CONF_TRANSPORT = "transport"
CONF_STORE_CREDENTIALS = "store_credentials"
CONF_HOST = "host"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_BABY_UID = "baby_uid"
CONF_CAMERA_UID = "camera_uid"
CONF_BABY_NAME = "baby_name"

# Transport Options
TRANSPORT_LOCAL = "local"
TRANSPORT_LOCAL_CLOUD = "local_cloud"

# Defaults
DEFAULT_HOST = "http://localhost:8080"
DEFAULT_SCAN_INTERVAL = 30
NANIT_API_BASE = "https://api.nanit.com"
