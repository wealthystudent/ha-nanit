from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DeviceStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    Disconnected: _ClassVar[DeviceStatus]
    Connected: _ClassVar[DeviceStatus]

class StateOfCharge(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SoCLow: _ClassVar[StateOfCharge]
    SoC25: _ClassVar[StateOfCharge]
    SoC50: _ClassVar[StateOfCharge]
    SoC75: _ClassVar[StateOfCharge]
    SoC90: _ClassVar[StateOfCharge]
Disconnected: DeviceStatus
Connected: DeviceStatus
SoCLow: StateOfCharge
SoC25: StateOfCharge
SoC50: StateOfCharge
SoC75: StateOfCharge
SoC90: StateOfCharge

class Message(_message.Message):
    __slots__ = ("request", "response", "backend")
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    request: Request
    response: Response
    backend: Backend
    def __init__(self, request: _Optional[_Union[Request, _Mapping]] = ..., response: _Optional[_Union[Response, _Mapping]] = ..., backend: _Optional[_Union[Backend, _Mapping]] = ...) -> None: ...

class Backend(_message.Message):
    __slots__ = ("device",)
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    device: BackendDevice
    def __init__(self, device: _Optional[_Union[BackendDevice, _Mapping]] = ...) -> None: ...

class BackendDevice(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: DeviceStatus
    def __init__(self, status: _Optional[_Union[DeviceStatus, str]] = ...) -> None: ...

class Request(_message.Message):
    __slots__ = ("id", "sessionId", "network", "firmware", "getSettings", "settings", "status", "getStatus")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSIONID_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    FIRMWARE_FIELD_NUMBER: _ClassVar[int]
    GETSETTINGS_FIELD_NUMBER: _ClassVar[int]
    SETTINGS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    GETSTATUS_FIELD_NUMBER: _ClassVar[int]
    id: int
    sessionId: str
    network: Network
    firmware: Firmware
    getSettings: GetSettings
    settings: Settings
    status: Status
    getStatus: GetStatus
    def __init__(self, id: _Optional[int] = ..., sessionId: _Optional[str] = ..., network: _Optional[_Union[Network, _Mapping]] = ..., firmware: _Optional[_Union[Firmware, _Mapping]] = ..., getSettings: _Optional[_Union[GetSettings, _Mapping]] = ..., settings: _Optional[_Union[Settings, _Mapping]] = ..., status: _Optional[_Union[Status, _Mapping]] = ..., getStatus: _Optional[_Union[GetStatus, _Mapping]] = ...) -> None: ...

class Response(_message.Message):
    __slots__ = ("requestId", "statusCode", "statusMessage", "settings", "firmware", "networkStatus", "status")
    REQUESTID_FIELD_NUMBER: _ClassVar[int]
    STATUSCODE_FIELD_NUMBER: _ClassVar[int]
    STATUSMESSAGE_FIELD_NUMBER: _ClassVar[int]
    SETTINGS_FIELD_NUMBER: _ClassVar[int]
    FIRMWARE_FIELD_NUMBER: _ClassVar[int]
    NETWORKSTATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    requestId: int
    statusCode: int
    statusMessage: str
    settings: Settings
    firmware: FirmwareInfo
    networkStatus: NetworkStatus
    status: Status
    def __init__(self, requestId: _Optional[int] = ..., statusCode: _Optional[int] = ..., statusMessage: _Optional[str] = ..., settings: _Optional[_Union[Settings, _Mapping]] = ..., firmware: _Optional[_Union[FirmwareInfo, _Mapping]] = ..., networkStatus: _Optional[_Union[NetworkStatus, _Mapping]] = ..., status: _Optional[_Union[Status, _Mapping]] = ...) -> None: ...

class GetSettings(_message.Message):
    __slots__ = ("all", "savedSounds", "temperature", "humidity")
    ALL_FIELD_NUMBER: _ClassVar[int]
    SAVEDSOUNDS_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    HUMIDITY_FIELD_NUMBER: _ClassVar[int]
    all: bool
    savedSounds: bool
    temperature: bool
    humidity: bool
    def __init__(self, all: bool = ..., savedSounds: bool = ..., temperature: bool = ..., humidity: bool = ...) -> None: ...

class Settings(_message.Message):
    __slots__ = ("brightness", "color", "volume", "sound", "isOn", "soundList", "temperature", "humidity")
    BRIGHTNESS_FIELD_NUMBER: _ClassVar[int]
    COLOR_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    SOUND_FIELD_NUMBER: _ClassVar[int]
    ISON_FIELD_NUMBER: _ClassVar[int]
    SOUNDLIST_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    HUMIDITY_FIELD_NUMBER: _ClassVar[int]
    brightness: float
    color: Color
    volume: float
    sound: Sound
    isOn: bool
    soundList: SoundList
    temperature: float
    humidity: float
    def __init__(self, brightness: _Optional[float] = ..., color: _Optional[_Union[Color, _Mapping]] = ..., volume: _Optional[float] = ..., sound: _Optional[_Union[Sound, _Mapping]] = ..., isOn: bool = ..., soundList: _Optional[_Union[SoundList, _Mapping]] = ..., temperature: _Optional[float] = ..., humidity: _Optional[float] = ...) -> None: ...

class Color(_message.Message):
    __slots__ = ("noColor", "hue", "saturation")
    NOCOLOR_FIELD_NUMBER: _ClassVar[int]
    HUE_FIELD_NUMBER: _ClassVar[int]
    SATURATION_FIELD_NUMBER: _ClassVar[int]
    noColor: bool
    hue: float
    saturation: float
    def __init__(self, noColor: bool = ..., hue: _Optional[float] = ..., saturation: _Optional[float] = ...) -> None: ...

class Sound(_message.Message):
    __slots__ = ("noSound", "track")
    NOSOUND_FIELD_NUMBER: _ClassVar[int]
    TRACK_FIELD_NUMBER: _ClassVar[int]
    noSound: bool
    track: str
    def __init__(self, noSound: bool = ..., track: _Optional[str] = ...) -> None: ...

class SoundList(_message.Message):
    __slots__ = ("tracks",)
    TRACKS_FIELD_NUMBER: _ClassVar[int]
    tracks: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tracks: _Optional[_Iterable[str]] = ...) -> None: ...

class Status(_message.Message):
    __slots__ = ("battery", "temperature", "humidity")
    BATTERY_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    HUMIDITY_FIELD_NUMBER: _ClassVar[int]
    battery: Battery
    temperature: float
    humidity: float
    def __init__(self, battery: _Optional[_Union[Battery, _Mapping]] = ..., temperature: _Optional[float] = ..., humidity: _Optional[float] = ...) -> None: ...

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetStatus(_message.Message):
    __slots__ = ("all", "battery", "temperature", "humidity")
    ALL_FIELD_NUMBER: _ClassVar[int]
    BATTERY_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    HUMIDITY_FIELD_NUMBER: _ClassVar[int]
    all: bool
    battery: bool
    temperature: bool
    humidity: bool
    def __init__(self, all: bool = ..., battery: bool = ..., temperature: bool = ..., humidity: bool = ...) -> None: ...

class Battery(_message.Message):
    __slots__ = ("soc", "isCharging")
    SOC_FIELD_NUMBER: _ClassVar[int]
    ISCHARGING_FIELD_NUMBER: _ClassVar[int]
    soc: StateOfCharge
    isCharging: bool
    def __init__(self, soc: _Optional[_Union[StateOfCharge, str]] = ..., isCharging: bool = ...) -> None: ...

class Firmware(_message.Message):
    __slots__ = ("info",)
    INFO_FIELD_NUMBER: _ClassVar[int]
    info: Empty
    def __init__(self, info: _Optional[_Union[Empty, _Mapping]] = ...) -> None: ...

class FirmwareInfo(_message.Message):
    __slots__ = ("url", "version")
    URL_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    url: str
    version: str
    def __init__(self, url: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class Network(_message.Message):
    __slots__ = ("getStatus",)
    GETSTATUS_FIELD_NUMBER: _ClassVar[int]
    getStatus: Empty
    def __init__(self, getStatus: _Optional[_Union[Empty, _Mapping]] = ...) -> None: ...

class NetworkStatus(_message.Message):
    __slots__ = ("currentAp",)
    CURRENTAP_FIELD_NUMBER: _ClassVar[int]
    currentAp: AccessPointInfo
    def __init__(self, currentAp: _Optional[_Union[AccessPointInfo, _Mapping]] = ...) -> None: ...

class AccessPointInfo(_message.Message):
    __slots__ = ("ssid", "bssid", "rssi", "primaryChannel")
    SSID_FIELD_NUMBER: _ClassVar[int]
    BSSID_FIELD_NUMBER: _ClassVar[int]
    RSSI_FIELD_NUMBER: _ClassVar[int]
    PRIMARYCHANNEL_FIELD_NUMBER: _ClassVar[int]
    ssid: str
    bssid: str
    rssi: int
    primaryChannel: int
    def __init__(self, ssid: _Optional[str] = ..., bssid: _Optional[str] = ..., rssi: _Optional[int] = ..., primaryChannel: _Optional[int] = ...) -> None: ...
