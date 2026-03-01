"""Data models for aionanit — frozen dataclasses representing camera state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum


class TransportKind(Enum):
    LOCAL = "local"
    CLOUD = "cloud"
    NONE = "none"


class ConnectionState(Enum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


class NightLightState(Enum):
    ON = "on"
    OFF = "off"


class SensorType(IntEnum):
    SOUND = 0
    MOTION = 1
    TEMPERATURE = 2
    HUMIDITY = 3
    LIGHT = 4
    NIGHT = 5


@dataclass(frozen=True)
class SensorReading:
    sensor_type: SensorType
    value: int | None = None
    value_milli: int | None = None
    is_alert: bool = False
    timestamp: int | None = None


@dataclass(frozen=True)
class SensorState:
    temperature: float | None = None    # Celsius, from value_milli / 1000
    humidity: float | None = None       # Percentage
    light: int | None = None            # Lux
    sound_alert: bool = False
    motion_alert: bool = False
    night: bool = False                 # True = dark / night mode active


@dataclass(frozen=True)
class SettingsState:
    night_vision: bool | None = None
    volume: int | None = None           # 0-100
    sleep_mode: bool | None = None
    status_light_on: bool | None = None
    mic_mute_on: bool | None = None
    wifi_band: str | None = None        # "any", "2.4ghz", "5ghz"
    mounting_mode: str | None = None    # "stand", "travel", "switch"


@dataclass(frozen=True)
class ControlState:
    night_light: NightLightState | None = None
    night_light_timeout: int | None = None
    sensor_data_transfer_enabled: bool | None = None


@dataclass(frozen=True)
class StatusState:
    connected_to_server: bool | None = None
    firmware_version: str | None = None
    hardware_version: str | None = None
    mounting_mode: str | None = None


@dataclass(frozen=True)
class ConnectionInfo:
    state: ConnectionState = ConnectionState.DISCONNECTED
    transport: TransportKind = TransportKind.NONE
    last_seen: datetime | None = None
    last_error: str | None = None
    reconnect_attempts: int = 0


@dataclass(frozen=True)
class CameraState:
    """Complete snapshot of everything known about one camera."""

    connection: ConnectionInfo = field(default_factory=ConnectionInfo)
    sensors: SensorState = field(default_factory=SensorState)
    settings: SettingsState = field(default_factory=SettingsState)
    control: ControlState = field(default_factory=ControlState)
    status: StatusState = field(default_factory=StatusState)


class CameraEventKind(Enum):
    SENSOR_UPDATE = "sensor_update"
    SETTINGS_UPDATE = "settings_update"
    CONTROL_UPDATE = "control_update"
    STATUS_UPDATE = "status_update"
    CONNECTION_CHANGE = "connection_change"


@dataclass(frozen=True)
class CameraEvent:
    kind: CameraEventKind
    state: CameraState  # Full state snapshot after event


@dataclass(frozen=True)
class Baby:
    uid: str
    name: str
    camera_uid: str


@dataclass(frozen=True)
class CloudEvent:
    """Event from the Nanit cloud API (motion/sound notifications)."""

    event_type: str     # "MOTION", "SOUND", etc.
    timestamp: float    # Unix timestamp
    baby_uid: str
