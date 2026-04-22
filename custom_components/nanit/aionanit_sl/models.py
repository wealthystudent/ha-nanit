"""Data models for the Sound & Light Machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class SoundLightRoutine:
    """A single routine definition from the S&L device."""

    name: str
    sound_name: str | None = None
    volume: float | None = None
    brightness: float | None = None


@dataclass(frozen=True)
class SoundLightFullState:
    """Complete state snapshot from the S&L device WebSocket.

    All float values are 0.0-1.0 scale as sent by the device.
    """

    # Light
    brightness: float | None = None
    light_enabled: bool | None = None
    color_r: float | None = None
    color_g: float | None = None

    # Sound
    sound_on: bool | None = None
    current_track: str | None = None
    volume: float | None = None
    available_tracks: tuple[str, ...] = ()

    # Power
    power_on: bool | None = None

    # Sensors
    temperature_c: float | None = None
    humidity_pct: float | None = None

    # Routines
    routines: tuple[SoundLightRoutine, ...] = ()

    # Timezone
    timezone_rule: str | None = None


class SoundLightEventKind(Enum):
    STATE_UPDATE = "state_update"
    SENSOR_UPDATE = "sensor_update"
    ROUTINES_UPDATE = "routines_update"
    CONNECTION_CHANGE = "connection_change"


@dataclass(frozen=True)
class SoundLightEvent:
    kind: SoundLightEventKind
    state: SoundLightFullState
