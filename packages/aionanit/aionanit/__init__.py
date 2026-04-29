"""aionanit — async Python client for Nanit baby cameras."""

from .auth import TokenManager
from .camera import NanitCamera
from .client import NanitClient
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
    NetworkInfo,
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
    # models
    "Baby",
    "CameraEvent",
    "CameraEventKind",
    "CameraState",
    "CloudEvent",
    "ConnectionInfo",
    "ConnectionState",
    "ControlState",
    # exceptions
    "NanitAuthError",
    # camera
    "NanitCamera",
    "NanitCameraUnavailable",
    # client
    "NanitClient",
    "NanitConnectionError",
    "NanitError",
    "NanitMfaRequiredError",
    "NanitProtocolError",
    "NanitRequestTimeout",
    # rest
    "NanitRestClient",
    "NanitTransportError",
    "NetworkInfo",
    "NightLightState",
    "SensorReading",
    "SensorState",
    "SensorType",
    "SettingsState",
    "StatusState",
    # auth
    "TokenManager",
    "TransportKind",
]
