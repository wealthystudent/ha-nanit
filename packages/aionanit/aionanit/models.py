"""Data models for aionanit — frozen dataclasses representing camera state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
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
    temperature: float | None = None  # Celsius, from value_milli / 1000
    humidity: float | None = None  # Percentage
    light: int | None = None  # Lux
    sound_alert: bool = False
    motion_alert: bool = False
    night: bool = False  # True = dark / night mode active


@dataclass(frozen=True)
class SettingsState:
    night_vision: bool | None = None
    volume: int | None = None  # 0-100
    sleep_mode: bool | None = None
    status_light_on: bool | None = None
    mic_mute_on: bool | None = None
    wifi_band: str | None = None  # "any", "2.4ghz", "5ghz"
    mounting_mode: str | None = None  # "stand", "travel", "switch"
    night_light_brightness: int | None = None  # 0-100


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
class PlaybackState:
    """Current sound machine playback state."""

    playing: bool = False
    current_track: str | None = None
    available_tracks: tuple[str, ...] = ()


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
    playback: PlaybackState = field(default_factory=PlaybackState)


class CameraEventKind(Enum):
    SENSOR_UPDATE = "sensor_update"
    SETTINGS_UPDATE = "settings_update"
    CONTROL_UPDATE = "control_update"
    STATUS_UPDATE = "status_update"
    PLAYBACK_UPDATE = "playback_update"
    CONNECTION_CHANGE = "connection_change"


@dataclass(frozen=True)
class CameraEvent:
    kind: CameraEventKind
    state: CameraState  # Full state snapshot after event


@dataclass(frozen=True)
class NetworkInfo:
    """WiFi network information reported by the camera."""

    ssid: str | None = None
    frequency_mhz: int | None = None  # e.g. 5240
    signal_dbm: int | None = None  # e.g. -60


@dataclass(frozen=True)
class Baby:
    uid: str  # "" = placeholder for a speaker no baby is paired with
    name: str
    camera_uid: str  # "" = no camera paired (standalone Sound & Light baby)
    speaker_uid: str | None = None
    network: NetworkInfo | None = None
    camera_connected: bool | None = None  # True = camera online per Nanit cloud
    camera_last_seen: int | None = None  # Unix timestamp of last cloud contact
    # Where care log history can start. Personal, so kept out of repr.
    birth_date: date | None = field(default=None, repr=False)
    created_at: float | None = field(default=None, repr=False)  # Unix timestamp of the record


@dataclass(frozen=True, repr=False)
class BabiesFetch:
    """One /babies fetch: the babies it lists, and how many rows were skipped.

    ``dropped`` counts the rows skipped as malformed. When it is not 0 the
    list is not the whole account: a baby missing from it may still be
    there. Its printed form shows only the counts.
    """

    babies: tuple[Baby, ...]
    dropped: int

    def __repr__(self) -> str:
        return f"BabiesFetch(<{len(self.babies)} babies>, dropped={self.dropped})"


@dataclass(frozen=True)
class CloudEvent:
    """Event from the Nanit cloud API (motion/sound notifications)."""

    event_type: str  # "MOTION", "SOUND", etc.
    timestamp: float  # Unix timestamp
    baby_uid: str


@dataclass(frozen=True)
class CareLog:
    """An entry from the Nanit app's Care Logs.

    Parents log these by hand in the app: diaper changes, bottles, nursing,
    pumping and naps. The calendar also carries the camera's own sleep
    sessions (``auto_sleep``). Only the fields that apply to ``log_type``
    are set; the rest stay None.

    Numbers and short tokens only: the notes and names an entry can carry
    are never kept here (see ``CareLogDetail``). A measurement the row
    carried but that could not be used (negative, not a finite number, or
    above a ceiling no real entry reaches: 100,000 mL, or 7 days in
    seconds) is None like a missing one, and its field name is in
    ``invalid_fields``.
    """

    uid: str
    log_type: str  # "diaper_change", "bottle_feed", "nursing", "pumping", "sleep", ...
    timestamp: float  # Unix timestamp of the start of the entry
    baby_uid: str
    change_type: str | None = None  # diaper_change: "pee", "poo", "mixed" or "dry"
    feed_amount_ml: float | None = None  # bottle_feed
    left_duration_s: float | None = None  # nursing
    right_duration_s: float | None = None  # nursing
    left_amount_ml: float | None = None  # pumping
    right_amount_ml: float | None = None  # pumping
    duration_s: float | None = None  # pumping, sleep, auto_sleep
    end_timestamp: float | None = None  # Unix timestamp of the end, when the row gives one
    net_duration_s: float | None = None  # sleep, semi_auto_sleep
    status: str | None = None  # auto_sleep: "eligible" or "not_eligible"
    logged_at: float | None = None  # Unix timestamp of when the entry was logged
    start_source: str | None = None  # semi_auto_sleep: "camera", "user", ...
    end_source: str | None = None  # semi_auto_sleep
    invalid_fields: frozenset[str] = frozenset()  # measurement fields present but rejected


@dataclass(frozen=True, repr=False)
class CareLogFetch:
    """One calendar fetch: its entries, the range asked for, and what was skipped.

    ``start`` and ``end`` are the Unix times sent; Nanit includes both.
    ``dropped`` counts the rows skipped as malformed (no uid, an unsafe or
    missing type, no time in Unix seconds, or not an object at all). Its
    printed form shows only the counts, none of the entries' ids, times or
    measurements.
    """

    logs: tuple[CareLog, ...]
    start: int
    end: int
    dropped: int

    def __repr__(self) -> str:
        return f"CareLogFetch(<{len(self.logs)} entries>, dropped={self.dropped})"


@dataclass(frozen=True)
class CareLogDetail:
    """The personal text of a Care Log entry: its note, and who logged it.

    Only ``async_get_calendar_details`` builds this; the numbers-only
    fetches never do. Each field is cleaned text (control and format
    characters removed, whitespace collapsed, length capped) or None. The
    text is kept out of ``repr``, so printing or logging one shows nothing
    of it.
    """

    comment: str | None = field(default=None, repr=False)
    user_name: str | None = field(default=None, repr=False)
    user_relation: str | None = field(default=None, repr=False)


@dataclass(frozen=True, repr=False)
class CareLogEntry:
    """A Care Log entry together with its personal text.

    Its printed form shows only the entry's kind, none of its ids, times or
    measurements.
    """

    log: CareLog
    detail: CareLogDetail

    def __repr__(self) -> str:
        return f"CareLogEntry(<{self.log.log_type}>)"


@dataclass(frozen=True, repr=False)
class CareLogDetailFetch:
    """One calendar fetch with personal text, otherwise like ``CareLogFetch``.

    Its printed form shows only the counts.
    """

    entries: tuple[CareLogEntry, ...]
    start: int
    end: int
    dropped: int

    def __repr__(self) -> str:
        return f"CareLogDetailFetch(<{len(self.entries)} entries>, dropped={self.dropped})"


@dataclass(frozen=True)
class SpeakerCandidate:
    """A Sound & Light Machine as the app's pairing view lists it.

    The view is per baby but covers every speaker on the account, which
    makes it the only way to find a machine no baby is paired with:
    /babies carries at most one speaker per baby.
    """

    uid: str
    name: str
    status: str  # "currently-coupled", "already-coupled" or "available"


@dataclass(frozen=True)
class AccountSpeaker:
    """A Sound & Light Machine as GET /speakers lists it.

    Account-wide and needs no baby, so it also finds a machine set up only
    in the Nanit Sound + Light app, on an account with no baby at all.
    """

    uid: str
    name: str
    attached_to_baby: bool


@dataclass(frozen=True)
class WebRtcCandidate:
    """An ICE candidate the camera trickled for a WebRTC session."""

    session_id: int
    candidate: str
    sdp_mid: str | None
    sdp_m_line_index: int | None


@dataclass(frozen=True)
class WebRtcClosed:
    """A WebRTC session ended by the camera, or by losing the control socket.

    ``error`` is a ``WebRtc.Error`` value (0 is a clean close).
    """

    session_id: int
    error: int
    error_message: str
