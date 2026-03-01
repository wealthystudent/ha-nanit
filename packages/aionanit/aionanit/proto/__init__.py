"""Re-exports of generated protobuf types with stable aliases.

Google protobuf nests enum and sub-message types inside parent messages
(e.g., ``Control.LIGHT_ON``, ``Settings.SensorSettings``).
This module provides flat aliases that match the names used throughout
the codebase so consumers don't need to know the nesting.
"""

from .nanit_pb2 import (  # noqa: F401
    Control,
    GetControl,
    GetLogs,
    GetSensorData,
    GetStatus,
    Message,
    MountingMode,
    Playback,
    Request,
    RequestType,
    Response,
    SensorData,
    SensorType,
    Settings,
    Status,
    Stream,
    StreamIdentifier,
    Streaming,
)

# ---------------------------------------------------------------------------
# Enum aliases — keep the flat names the rest of the codebase expects.
# Google protobuf puts nested enum *values* directly on the parent class
# (e.g. ``Control.LIGHT_ON``).  The wrapper classes below group those
# values under a dedicated name so call-sites read naturally.
# ---------------------------------------------------------------------------


class MessageType:
    """Message.Type enum values."""

    KEEPALIVE: int = Message.KEEPALIVE
    REQUEST: int = Message.REQUEST
    RESPONSE: int = Message.RESPONSE


class ControlNightLight:
    """Control.NightLight enum values."""

    LIGHT_OFF: int = Control.LIGHT_OFF
    LIGHT_ON: int = Control.LIGHT_ON


class SettingsAntiFlicker:
    """Settings.AntiFlicker enum values."""

    FR50HZ: int = Settings.FR50HZ
    FR60HZ: int = Settings.FR60HZ


class SettingsWifiBand:
    """Settings.WifiBand enum values."""

    ANY: int = Settings.ANY
    FR2_4GHZ: int = Settings.FR2_4GHZ
    FR5_0GHZ: int = Settings.FR5_0GHZ


class StatusConnectionToServer:
    """Status.ConnectionToServer enum values."""

    DISCONNECTED: int = Status.DISCONNECTED
    CONNECTED: int = Status.CONNECTED


class PlaybackStatus:
    """Playback.Status enum values."""

    STARTED: int = Playback.STARTED
    STOPPED: int = Playback.STOPPED


class StreamType:
    """Stream.Type enum values."""

    LOCAL: int = Stream.LOCAL
    REMOTE: int = Stream.REMOTE
    RTSP: int = Stream.RTSP
    P2P: int = Stream.P2P


class StreamingStatus:
    """Streaming.Status enum values."""

    STARTED: int = Streaming.STARTED
    STOPPED: int = Streaming.STOPPED
    PAUSED: int = Streaming.PAUSED


# ---------------------------------------------------------------------------
# Nested message aliases — flat names for sub-messages.
# ---------------------------------------------------------------------------

ControlSensorDataTransfer = Control.SensorDataTransfer
SettingsSensorSettings = Settings.SensorSettings
SettingsStreamSettings = Settings.StreamSettings


__all__ = [
    "Control",
    "ControlNightLight",
    "ControlSensorDataTransfer",
    "GetControl",
    "GetLogs",
    "GetSensorData",
    "GetStatus",
    "Message",
    "MessageType",
    "MountingMode",
    "Playback",
    "PlaybackStatus",
    "Request",
    "RequestType",
    "Response",
    "SensorData",
    "SensorType",
    "Settings",
    "SettingsAntiFlicker",
    "SettingsSensorSettings",
    "SettingsStreamSettings",
    "SettingsWifiBand",
    "Status",
    "StatusConnectionToServer",
    "Stream",
    "StreamIdentifier",
    "StreamType",
    "Streaming",
    "StreamingStatus",
]
