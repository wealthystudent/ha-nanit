"""aionanit — async Python client for Nanit baby cameras."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# Lightweight — no protobuf dependency.
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
    PlaybackState,
    SensorReading,
    SensorState,
    SensorType,
    SettingsState,
    StatusState,
    TransportKind,
)

if TYPE_CHECKING:
    from .auth import TokenManager as TokenManager
    from .camera import NanitCamera as NanitCamera
    from .client import NanitClient as NanitClient
    from .rest import NanitRestClient as NanitRestClient

# Heavy symbols — lazily imported on first access so that
# ``import aionanit`` does not pull in the protobuf chain
# (camera → parsers → proto → nanit_pb2 → google.protobuf).
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "NanitCamera": (".camera", "NanitCamera"),
    "NanitClient": (".client", "NanitClient"),
    "NanitRestClient": (".rest", "NanitRestClient"),
    "TokenManager": (".auth", "TokenManager"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        mod = importlib.import_module(module_path, __name__)
        value = getattr(mod, attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return __all__


__all__ = [
    "Baby",
    "CameraEvent",
    "CameraEventKind",
    "CameraState",
    "CloudEvent",
    "ConnectionInfo",
    "ConnectionState",
    "ControlState",
    "NanitAuthError",
    "NanitCamera",
    "NanitCameraUnavailable",
    "NanitClient",
    "NanitConnectionError",
    "NanitError",
    "NanitMfaRequiredError",
    "NanitProtocolError",
    "NanitRequestTimeout",
    "NanitRestClient",
    "NanitTransportError",
    "NetworkInfo",
    "NightLightState",
    "PlaybackState",
    "SensorReading",
    "SensorState",
    "SensorType",
    "SettingsState",
    "StatusState",
    "TokenManager",
    "TransportKind",
]
