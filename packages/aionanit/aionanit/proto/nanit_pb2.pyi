from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RequestType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PUT_STREAMING: _ClassVar[RequestType]
    GET_STREAMING: _ClassVar[RequestType]
    GET_SETTINGS: _ClassVar[RequestType]
    PUT_SETTINGS: _ClassVar[RequestType]
    GET_CONTROL: _ClassVar[RequestType]
    PUT_CONTROL: _ClassVar[RequestType]
    GET_STATUS: _ClassVar[RequestType]
    PUT_STATUS: _ClassVar[RequestType]
    PUT_SENSOR_DATA: _ClassVar[RequestType]
    GET_SENSOR_DATA: _ClassVar[RequestType]
    GET_UCTOKENS: _ClassVar[RequestType]
    PUT_UCTOKENS: _ClassVar[RequestType]
    PUT_SETUP_NETWORK: _ClassVar[RequestType]
    PUT_SETUP_SERVER: _ClassVar[RequestType]
    GET_FIRMWARE: _ClassVar[RequestType]
    PUT_FIRMWARE: _ClassVar[RequestType]
    GET_PLAYBACK: _ClassVar[RequestType]
    PUT_PLAYBACK: _ClassVar[RequestType]
    GET_SOUNDTRACKS: _ClassVar[RequestType]
    GET_STATUS_NETWORK: _ClassVar[RequestType]
    GET_LIST_NETWORKS: _ClassVar[RequestType]
    GET_LOGS: _ClassVar[RequestType]
    GET_BANDWIDTH: _ClassVar[RequestType]
    GET_AUDIO_STREAMING: _ClassVar[RequestType]
    PUT_AUDIO_STREAMING: _ClassVar[RequestType]
    GET_WIFI_SETUP: _ClassVar[RequestType]
    PUT_WIFI_SETUP: _ClassVar[RequestType]
    PUT_STING_START: _ClassVar[RequestType]
    PUT_STING_STOP: _ClassVar[RequestType]
    PUT_STING_STATUS: _ClassVar[RequestType]
    PUT_STING_ALERT: _ClassVar[RequestType]
    PUT_KEEP_ALIVE: _ClassVar[RequestType]
    GET_STING_STATUS: _ClassVar[RequestType]
    PUT_STING_TEST: _ClassVar[RequestType]
    PUT_RTSP_STREAMING: _ClassVar[RequestType]
    GET_UOM_URI: _ClassVar[RequestType]
    GET_UOM: _ClassVar[RequestType]
    PUT_UOM: _ClassVar[RequestType]
    GET_AUTH_KEY: _ClassVar[RequestType]
    PUT_AUTH_KEY: _ClassVar[RequestType]
    PUT_HEALTH: _ClassVar[RequestType]
    PUT_TCP_REQUEST: _ClassVar[RequestType]
    GET_STING_START: _ClassVar[RequestType]
    GET_LOGS_URI: _ClassVar[RequestType]

class SensorType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SOUND: _ClassVar[SensorType]
    MOTION: _ClassVar[SensorType]
    TEMPERATURE: _ClassVar[SensorType]
    HUMIDITY: _ClassVar[SensorType]
    LIGHT: _ClassVar[SensorType]
    NIGHT: _ClassVar[SensorType]

class StreamIdentifier(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DVR: _ClassVar[StreamIdentifier]
    ANALYTICS: _ClassVar[StreamIdentifier]
    MOBILE: _ClassVar[StreamIdentifier]

class MountingMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STAND: _ClassVar[MountingMode]
    TRAVEL: _ClassVar[MountingMode]
    SWITCH: _ClassVar[MountingMode]

PUT_STREAMING: RequestType
GET_STREAMING: RequestType
GET_SETTINGS: RequestType
PUT_SETTINGS: RequestType
GET_CONTROL: RequestType
PUT_CONTROL: RequestType
GET_STATUS: RequestType
PUT_STATUS: RequestType
PUT_SENSOR_DATA: RequestType
GET_SENSOR_DATA: RequestType
GET_UCTOKENS: RequestType
PUT_UCTOKENS: RequestType
PUT_SETUP_NETWORK: RequestType
PUT_SETUP_SERVER: RequestType
GET_FIRMWARE: RequestType
PUT_FIRMWARE: RequestType
GET_PLAYBACK: RequestType
PUT_PLAYBACK: RequestType
GET_SOUNDTRACKS: RequestType
GET_STATUS_NETWORK: RequestType
GET_LIST_NETWORKS: RequestType
GET_LOGS: RequestType
GET_BANDWIDTH: RequestType
GET_AUDIO_STREAMING: RequestType
PUT_AUDIO_STREAMING: RequestType
GET_WIFI_SETUP: RequestType
PUT_WIFI_SETUP: RequestType
PUT_STING_START: RequestType
PUT_STING_STOP: RequestType
PUT_STING_STATUS: RequestType
PUT_STING_ALERT: RequestType
PUT_KEEP_ALIVE: RequestType
GET_STING_STATUS: RequestType
PUT_STING_TEST: RequestType
PUT_RTSP_STREAMING: RequestType
GET_UOM_URI: RequestType
GET_UOM: RequestType
PUT_UOM: RequestType
GET_AUTH_KEY: RequestType
PUT_AUTH_KEY: RequestType
PUT_HEALTH: RequestType
PUT_TCP_REQUEST: RequestType
GET_STING_START: RequestType
GET_LOGS_URI: RequestType
SOUND: SensorType
MOTION: SensorType
TEMPERATURE: SensorType
HUMIDITY: SensorType
LIGHT: SensorType
NIGHT: SensorType
DVR: StreamIdentifier
ANALYTICS: StreamIdentifier
MOBILE: StreamIdentifier
STAND: MountingMode
TRAVEL: MountingMode
SWITCH: MountingMode

class SensorData(_message.Message):
    __slots__ = ("sensor_type", "value", "is_alert", "timestamp", "value_milli")
    SENSOR_TYPE_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    IS_ALERT_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    VALUE_MILLI_FIELD_NUMBER: _ClassVar[int]
    sensor_type: SensorType
    value: int
    is_alert: bool
    timestamp: int
    value_milli: int
    def __init__(
        self,
        sensor_type: _Optional[_Union[SensorType, str]] = ...,
        value: _Optional[int] = ...,
        is_alert: _Optional[bool] = ...,
        timestamp: _Optional[int] = ...,
        value_milli: _Optional[int] = ...,
    ) -> None: ...

class GetSensorData(_message.Message):
    __slots__ = ("all", "temperature", "humidity", "light", "night")
    ALL_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    HUMIDITY_FIELD_NUMBER: _ClassVar[int]
    LIGHT_FIELD_NUMBER: _ClassVar[int]
    NIGHT_FIELD_NUMBER: _ClassVar[int]
    all: bool
    temperature: bool
    humidity: bool
    light: bool
    night: bool
    def __init__(
        self,
        all: _Optional[bool] = ...,
        temperature: _Optional[bool] = ...,
        humidity: _Optional[bool] = ...,
        light: _Optional[bool] = ...,
        night: _Optional[bool] = ...,
    ) -> None: ...

class GetControl(_message.Message):
    __slots__ = ("ptz", "night_light", "night_light_timeout", "sensor_data_transfer_en")
    PTZ_FIELD_NUMBER: _ClassVar[int]
    NIGHT_LIGHT_FIELD_NUMBER: _ClassVar[int]
    NIGHT_LIGHT_TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    SENSOR_DATA_TRANSFER_EN_FIELD_NUMBER: _ClassVar[int]
    ptz: bool
    night_light: bool
    night_light_timeout: bool
    sensor_data_transfer_en: bool
    def __init__(
        self,
        ptz: _Optional[bool] = ...,
        night_light: _Optional[bool] = ...,
        night_light_timeout: _Optional[bool] = ...,
        sensor_data_transfer_en: _Optional[bool] = ...,
    ) -> None: ...

class Control(_message.Message):
    __slots__ = (
        "night_light",
        "sensor_data_transfer",
        "force_connect_to_server",
        "night_light_timeout",
    )
    class NightLight(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        LIGHT_OFF: _ClassVar[Control.NightLight]
        LIGHT_ON: _ClassVar[Control.NightLight]

    LIGHT_OFF: Control.NightLight
    LIGHT_ON: Control.NightLight
    class SensorDataTransfer(_message.Message):
        __slots__ = ("sound", "motion", "temperature", "humidity", "light", "night")
        SOUND_FIELD_NUMBER: _ClassVar[int]
        MOTION_FIELD_NUMBER: _ClassVar[int]
        TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
        HUMIDITY_FIELD_NUMBER: _ClassVar[int]
        LIGHT_FIELD_NUMBER: _ClassVar[int]
        NIGHT_FIELD_NUMBER: _ClassVar[int]
        sound: bool
        motion: bool
        temperature: bool
        humidity: bool
        light: bool
        night: bool
        def __init__(
            self,
            sound: _Optional[bool] = ...,
            motion: _Optional[bool] = ...,
            temperature: _Optional[bool] = ...,
            humidity: _Optional[bool] = ...,
            light: _Optional[bool] = ...,
            night: _Optional[bool] = ...,
        ) -> None: ...

    NIGHT_LIGHT_FIELD_NUMBER: _ClassVar[int]
    SENSOR_DATA_TRANSFER_FIELD_NUMBER: _ClassVar[int]
    FORCE_CONNECT_TO_SERVER_FIELD_NUMBER: _ClassVar[int]
    NIGHT_LIGHT_TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    night_light: Control.NightLight
    sensor_data_transfer: Control.SensorDataTransfer
    force_connect_to_server: bool
    night_light_timeout: int
    def __init__(
        self,
        night_light: _Optional[_Union[Control.NightLight, str]] = ...,
        sensor_data_transfer: _Optional[_Union[Control.SensorDataTransfer, _Mapping]] = ...,
        force_connect_to_server: _Optional[bool] = ...,
        night_light_timeout: _Optional[int] = ...,
    ) -> None: ...

class Settings(_message.Message):
    __slots__ = (
        "night_vision",
        "sensors",
        "streams",
        "volume",
        "anti_flicker",
        "sleep_mode",
        "status_light_on",
        "mounting_mode",
        "wifi_band",
        "mic_mute_on",
        "night_light_brightness",
    )
    class AntiFlicker(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        FR50HZ: _ClassVar[Settings.AntiFlicker]
        FR60HZ: _ClassVar[Settings.AntiFlicker]

    FR50HZ: Settings.AntiFlicker
    FR60HZ: Settings.AntiFlicker
    class WifiBand(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        ANY: _ClassVar[Settings.WifiBand]
        FR2_4GHZ: _ClassVar[Settings.WifiBand]
        FR5_0GHZ: _ClassVar[Settings.WifiBand]

    ANY: Settings.WifiBand
    FR2_4GHZ: Settings.WifiBand
    FR5_0GHZ: Settings.WifiBand
    class SensorSettings(_message.Message):
        __slots__ = (
            "sensor_type",
            "use_low_threshold",
            "use_high_threshold",
            "low_threshold",
            "high_threshold",
            "sample_interval_sec",
            "trigger_interval_sec",
            "use_milli_for_thresholds",
        )
        SENSOR_TYPE_FIELD_NUMBER: _ClassVar[int]
        USE_LOW_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
        USE_HIGH_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
        LOW_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
        HIGH_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
        SAMPLE_INTERVAL_SEC_FIELD_NUMBER: _ClassVar[int]
        TRIGGER_INTERVAL_SEC_FIELD_NUMBER: _ClassVar[int]
        USE_MILLI_FOR_THRESHOLDS_FIELD_NUMBER: _ClassVar[int]
        sensor_type: SensorType
        use_low_threshold: bool
        use_high_threshold: bool
        low_threshold: int
        high_threshold: int
        sample_interval_sec: int
        trigger_interval_sec: int
        use_milli_for_thresholds: bool
        def __init__(
            self,
            sensor_type: _Optional[_Union[SensorType, str]] = ...,
            use_low_threshold: _Optional[bool] = ...,
            use_high_threshold: _Optional[bool] = ...,
            low_threshold: _Optional[int] = ...,
            high_threshold: _Optional[int] = ...,
            sample_interval_sec: _Optional[int] = ...,
            trigger_interval_sec: _Optional[int] = ...,
            use_milli_for_thresholds: _Optional[bool] = ...,
        ) -> None: ...

    class StreamSettings(_message.Message):
        __slots__ = ("id", "bitrate", "economy_bitrate", "economy_fps", "best_bitrate", "best_fps")
        ID_FIELD_NUMBER: _ClassVar[int]
        BITRATE_FIELD_NUMBER: _ClassVar[int]
        ECONOMY_BITRATE_FIELD_NUMBER: _ClassVar[int]
        ECONOMY_FPS_FIELD_NUMBER: _ClassVar[int]
        BEST_BITRATE_FIELD_NUMBER: _ClassVar[int]
        BEST_FPS_FIELD_NUMBER: _ClassVar[int]
        id: StreamIdentifier
        bitrate: int
        economy_bitrate: int
        economy_fps: int
        best_bitrate: int
        best_fps: int
        def __init__(
            self,
            id: _Optional[_Union[StreamIdentifier, str]] = ...,
            bitrate: _Optional[int] = ...,
            economy_bitrate: _Optional[int] = ...,
            economy_fps: _Optional[int] = ...,
            best_bitrate: _Optional[int] = ...,
            best_fps: _Optional[int] = ...,
        ) -> None: ...

    NIGHT_VISION_FIELD_NUMBER: _ClassVar[int]
    SENSORS_FIELD_NUMBER: _ClassVar[int]
    STREAMS_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    ANTI_FLICKER_FIELD_NUMBER: _ClassVar[int]
    SLEEP_MODE_FIELD_NUMBER: _ClassVar[int]
    STATUS_LIGHT_ON_FIELD_NUMBER: _ClassVar[int]
    MOUNTING_MODE_FIELD_NUMBER: _ClassVar[int]
    WIFI_BAND_FIELD_NUMBER: _ClassVar[int]
    MIC_MUTE_ON_FIELD_NUMBER: _ClassVar[int]
    NIGHT_LIGHT_BRIGHTNESS_FIELD_NUMBER: _ClassVar[int]
    night_vision: bool
    sensors: _containers.RepeatedCompositeFieldContainer[Settings.SensorSettings]
    streams: _containers.RepeatedCompositeFieldContainer[Settings.StreamSettings]
    volume: int
    anti_flicker: Settings.AntiFlicker
    sleep_mode: bool
    status_light_on: bool
    mounting_mode: int
    wifi_band: Settings.WifiBand
    mic_mute_on: bool
    night_light_brightness: int
    def __init__(
        self,
        night_vision: _Optional[bool] = ...,
        sensors: _Optional[_Iterable[_Union[Settings.SensorSettings, _Mapping]]] = ...,
        streams: _Optional[_Iterable[_Union[Settings.StreamSettings, _Mapping]]] = ...,
        volume: _Optional[int] = ...,
        anti_flicker: _Optional[_Union[Settings.AntiFlicker, str]] = ...,
        sleep_mode: _Optional[bool] = ...,
        status_light_on: _Optional[bool] = ...,
        mounting_mode: _Optional[int] = ...,
        wifi_band: _Optional[_Union[Settings.WifiBand, str]] = ...,
        mic_mute_on: _Optional[bool] = ...,
        night_light_brightness: _Optional[int] = ...,
    ) -> None: ...

class Status(_message.Message):
    __slots__ = (
        "upgrade_downloaded",
        "connection_to_server",
        "current_version",
        "mode",
        "is_security_upgrade",
        "downloaded_version",
        "hardware_version",
    )
    class ConnectionToServer(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        DISCONNECTED: _ClassVar[Status.ConnectionToServer]
        CONNECTED: _ClassVar[Status.ConnectionToServer]

    DISCONNECTED: Status.ConnectionToServer
    CONNECTED: Status.ConnectionToServer
    UPGRADE_DOWNLOADED_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_TO_SERVER_FIELD_NUMBER: _ClassVar[int]
    CURRENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    IS_SECURITY_UPGRADE_FIELD_NUMBER: _ClassVar[int]
    DOWNLOADED_VERSION_FIELD_NUMBER: _ClassVar[int]
    HARDWARE_VERSION_FIELD_NUMBER: _ClassVar[int]
    upgrade_downloaded: bool
    connection_to_server: Status.ConnectionToServer
    current_version: str
    mode: MountingMode
    is_security_upgrade: bool
    downloaded_version: str
    hardware_version: str
    def __init__(
        self,
        upgrade_downloaded: _Optional[bool] = ...,
        connection_to_server: _Optional[_Union[Status.ConnectionToServer, str]] = ...,
        current_version: _Optional[str] = ...,
        mode: _Optional[_Union[MountingMode, str]] = ...,
        is_security_upgrade: _Optional[bool] = ...,
        downloaded_version: _Optional[str] = ...,
        hardware_version: _Optional[str] = ...,
    ) -> None: ...

class Soundtrack(_message.Message):
    __slots__ = ("type", "filename")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    FILENAME_FIELD_NUMBER: _ClassVar[int]
    type: int
    filename: str
    def __init__(self, type: _Optional[int] = ..., filename: _Optional[str] = ...) -> None: ...

class Playback(_message.Message):
    __slots__ = ("status", "duration", "track", "current")
    class Status(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        STARTED: _ClassVar[Playback.Status]
        STOPPED: _ClassVar[Playback.Status]

    STARTED: Playback.Status
    STOPPED: Playback.Status
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    TRACK_FIELD_NUMBER: _ClassVar[int]
    CURRENT_FIELD_NUMBER: _ClassVar[int]
    status: Playback.Status
    duration: int
    track: Soundtrack
    current: Soundtrack
    def __init__(
        self,
        status: _Optional[_Union[Playback.Status, str]] = ...,
        duration: _Optional[int] = ...,
        track: _Optional[_Union[Soundtrack, _Mapping]] = ...,
        current: _Optional[_Union[Soundtrack, _Mapping]] = ...,
    ) -> None: ...

class Stream(_message.Message):
    __slots__ = ("type", "url", "bps")
    class Type(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        LOCAL: _ClassVar[Stream.Type]
        REMOTE: _ClassVar[Stream.Type]
        RTSP: _ClassVar[Stream.Type]
        P2P: _ClassVar[Stream.Type]

    LOCAL: Stream.Type
    REMOTE: Stream.Type
    RTSP: Stream.Type
    P2P: Stream.Type
    TYPE_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    BPS_FIELD_NUMBER: _ClassVar[int]
    type: Stream.Type
    url: str
    bps: int
    def __init__(
        self,
        type: _Optional[_Union[Stream.Type, str]] = ...,
        url: _Optional[str] = ...,
        bps: _Optional[int] = ...,
    ) -> None: ...

class Streaming(_message.Message):
    __slots__ = ("id", "status", "rtmp_url", "attempts")
    class Status(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        STARTED: _ClassVar[Streaming.Status]
        STOPPED: _ClassVar[Streaming.Status]
        PAUSED: _ClassVar[Streaming.Status]

    STARTED: Streaming.Status
    STOPPED: Streaming.Status
    PAUSED: Streaming.Status
    ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RTMP_URL_FIELD_NUMBER: _ClassVar[int]
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    id: StreamIdentifier
    status: Streaming.Status
    rtmp_url: str
    attempts: int
    def __init__(
        self,
        id: _Optional[_Union[StreamIdentifier, str]] = ...,
        status: _Optional[_Union[Streaming.Status, str]] = ...,
        rtmp_url: _Optional[str] = ...,
        attempts: _Optional[int] = ...,
    ) -> None: ...

class GetLogs(_message.Message):
    __slots__ = ("url",)
    URL_FIELD_NUMBER: _ClassVar[int]
    url: str
    def __init__(self, url: _Optional[str] = ...) -> None: ...

class GetStatus(_message.Message):
    __slots__ = ("all",)
    ALL_FIELD_NUMBER: _ClassVar[int]
    all: bool
    def __init__(self, all: _Optional[bool] = ...) -> None: ...

class GetSettings(_message.Message):
    __slots__ = ("all",)
    ALL_FIELD_NUMBER: _ClassVar[int]
    all: bool
    def __init__(self, all: _Optional[bool] = ...) -> None: ...

class Request(_message.Message):
    __slots__ = (
        "id",
        "type",
        "streaming",
        "settings",
        "get_settings",
        "status",
        "get_status",
        "get_sensor_data",
        "sensor_data",
        "control",
        "playback",
        "get_control",
        "get_logs",
    )
    ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    STREAMING_FIELD_NUMBER: _ClassVar[int]
    SETTINGS_FIELD_NUMBER: _ClassVar[int]
    GET_SETTINGS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    GET_STATUS_FIELD_NUMBER: _ClassVar[int]
    GET_SENSOR_DATA_FIELD_NUMBER: _ClassVar[int]
    SENSOR_DATA_FIELD_NUMBER: _ClassVar[int]
    CONTROL_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_FIELD_NUMBER: _ClassVar[int]
    GET_CONTROL_FIELD_NUMBER: _ClassVar[int]
    GET_LOGS_FIELD_NUMBER: _ClassVar[int]
    id: int
    type: RequestType
    streaming: Streaming
    settings: Settings
    get_settings: GetSettings
    status: Status
    get_status: GetStatus
    get_sensor_data: GetSensorData
    sensor_data: _containers.RepeatedCompositeFieldContainer[SensorData]
    control: Control
    playback: Playback
    get_control: GetControl
    get_logs: GetLogs
    def __init__(
        self,
        id: _Optional[int] = ...,
        type: _Optional[_Union[RequestType, str]] = ...,
        streaming: _Optional[_Union[Streaming, _Mapping]] = ...,
        settings: _Optional[_Union[Settings, _Mapping]] = ...,
        get_settings: _Optional[_Union[GetSettings, _Mapping]] = ...,
        status: _Optional[_Union[Status, _Mapping]] = ...,
        get_status: _Optional[_Union[GetStatus, _Mapping]] = ...,
        get_sensor_data: _Optional[_Union[GetSensorData, _Mapping]] = ...,
        sensor_data: _Optional[_Iterable[_Union[SensorData, _Mapping]]] = ...,
        control: _Optional[_Union[Control, _Mapping]] = ...,
        playback: _Optional[_Union[Playback, _Mapping]] = ...,
        get_control: _Optional[_Union[GetControl, _Mapping]] = ...,
        get_logs: _Optional[_Union[GetLogs, _Mapping]] = ...,
    ) -> None: ...

class Response(_message.Message):
    __slots__ = (
        "request_id",
        "request_type",
        "status_code",
        "status_message",
        "status",
        "settings",
        "sensor_data",
        "playback",
        "soundtracks",
        "control",
    )
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_CODE_FIELD_NUMBER: _ClassVar[int]
    STATUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SETTINGS_FIELD_NUMBER: _ClassVar[int]
    SENSOR_DATA_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_FIELD_NUMBER: _ClassVar[int]
    SOUNDTRACKS_FIELD_NUMBER: _ClassVar[int]
    CONTROL_FIELD_NUMBER: _ClassVar[int]
    request_id: int
    request_type: RequestType
    status_code: int
    status_message: str
    status: Status
    settings: Settings
    sensor_data: _containers.RepeatedCompositeFieldContainer[SensorData]
    playback: Playback
    soundtracks: _containers.RepeatedCompositeFieldContainer[Soundtrack]
    control: Control
    def __init__(
        self,
        request_id: _Optional[int] = ...,
        request_type: _Optional[_Union[RequestType, str]] = ...,
        status_code: _Optional[int] = ...,
        status_message: _Optional[str] = ...,
        status: _Optional[_Union[Status, _Mapping]] = ...,
        settings: _Optional[_Union[Settings, _Mapping]] = ...,
        sensor_data: _Optional[_Iterable[_Union[SensorData, _Mapping]]] = ...,
        playback: _Optional[_Union[Playback, _Mapping]] = ...,
        soundtracks: _Optional[_Iterable[_Union[Soundtrack, _Mapping]]] = ...,
        control: _Optional[_Union[Control, _Mapping]] = ...,
    ) -> None: ...

class Message(_message.Message):
    __slots__ = ("type", "request", "response")
    class Type(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        KEEPALIVE: _ClassVar[Message.Type]
        REQUEST: _ClassVar[Message.Type]
        RESPONSE: _ClassVar[Message.Type]

    KEEPALIVE: Message.Type
    REQUEST: Message.Type
    RESPONSE: Message.Type
    TYPE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    type: Message.Type
    request: Request
    response: Response
    def __init__(
        self,
        type: _Optional[_Union[Message.Type, str]] = ...,
        request: _Optional[_Union[Request, _Mapping]] = ...,
        response: _Optional[_Union[Response, _Mapping]] = ...,
    ) -> None: ...
