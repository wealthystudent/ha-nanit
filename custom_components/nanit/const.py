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
]

# Cloud event detection window (seconds)
CLOUD_EVENT_WINDOW = 300

# Cloud event poll interval (seconds)
CLOUD_POLL_INTERVAL = 30

# Config Keys
CONF_MFA_CODE = "mfa_code"
CONF_MFA_TOKEN = "mfa_token"
CONF_STORE_CREDENTIALS = "store_credentials"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_BABY_UID = "baby_uid"
CONF_CAMERA_UID = "camera_uid"
CONF_BABY_NAME = "baby_name"
CONF_CAMERA_IP = "camera_ip"
