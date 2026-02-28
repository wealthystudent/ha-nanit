"""aionanit — async Python client for Nanit baby cameras."""

from .auth import TokenManager
from .exceptions import (
    NanitAuthError,
    NanitCameraUnavailable,
    NanitConnectionError,
    NanitError,
    NanitMfaRequiredError,
    NanitProtocolError,
    NanitRequestTimeout,
    NanitTransportError,
)
from .models import (
    Baby,
    CameraEvent,
    CameraEventKind,
    CameraState,
    CloudEvent,
    ConnectionInfo,
    ConnectionState,
    ControlState,
    NightLightState,
    SensorReading,
    SensorState,
    SensorType,
    SettingsState,
    StatusState,
    TransportKind,
)
from .rest import NanitRestClient

__all__ = [
    # auth
    "TokenManager",
    # rest
    "NanitRestClient",
    # models
    "Baby",
    "CameraEvent",
    "CameraEventKind",
    "CameraState",
    "CloudEvent",
    "ConnectionInfo",
    "ConnectionState",
    "ControlState",
    "NightLightState",
    "SensorReading",
    "SensorState",
    "SensorType",
    "SettingsState",
    "StatusState",
    "TransportKind",
    # exceptions
    "NanitAuthError",
    "NanitCameraUnavailable",
    "NanitConnectionError",
    "NanitError",
    "NanitMfaRequiredError",
    "NanitProtocolError",
    "NanitRequestTimeout",
    "NanitTransportError",
]
