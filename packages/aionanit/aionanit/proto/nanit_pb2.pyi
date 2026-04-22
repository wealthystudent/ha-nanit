"""Type stubs for generated protobuf module (nanit_pb2).

Auto-generated stubs — DO NOT EDIT by hand.
Regenerate via: scripts/generate_proto.py or mypy-protobuf.
"""

import google.protobuf.message

class Control(google.protobuf.message.Message):
    LIGHT_OFF: int
    LIGHT_ON: int
    class SensorDataTransfer(google.protobuf.message.Message): ...
    night_light: int
    sensor_data_transfer: Control.SensorDataTransfer
    force_connect_to_server: bool
    night_light_timeout: int

class GetControl(google.protobuf.message.Message):
    ptz: bool
    night_light: bool
    night_light_timeout: bool
    sensor_data_transfer_en: bool

class GetLogs(google.protobuf.message.Message):
    url: str

class GetSensorData(google.protobuf.message.Message):
    all: bool
    temperature: bool
    humidity: bool
    light: bool
    night: bool

class GetStatus(google.protobuf.message.Message):
    all: bool

class Message(google.protobuf.message.Message):
    KEEPALIVE: int
    REQUEST: int
    RESPONSE: int
    type: int
    request: Request
    response: Response
    @classmethod
    def FromString(cls, s: bytes) -> Message: ...

class MountingMode(google.protobuf.message.Message): ...

class Playback(google.protobuf.message.Message):
    STARTED: int
    STOPPED: int
    status: int

class Request(google.protobuf.message.Message):
    id: int
    type: int
    streaming: Streaming
    settings: Settings
    status: Status
    get_status: GetStatus
    get_sensor_data: GetSensorData
    sensor_data: SensorData
    control: Control
    playback: Playback
    get_control: GetControl
    get_logs: GetLogs

class RequestType:
    PUT_STREAMING: int
    GET_STREAMING: int
    GET_SETTINGS: int
    PUT_SETTINGS: int
    GET_CONTROL: int
    PUT_CONTROL: int
    GET_STATUS: int
    PUT_STATUS: int
    PUT_SENSOR_DATA: int
    GET_SENSOR_DATA: int
    GET_UCTOKENS: int
    PUT_UCTOKENS: int
    PUT_SETUP_NETWORK: int
    PUT_SETUP_SERVER: int
    GET_FIRMWARE: int
    PUT_FIRMWARE: int
    GET_PLAYBACK: int
    PUT_PLAYBACK: int
    GET_SOUNDTRACKS: int
    GET_STATUS_NETWORK: int
    GET_LIST_NETWORKS: int
    GET_LOGS: int
    GET_BANDWIDTH: int
    GET_AUDIO_STREAMING: int
    PUT_AUDIO_STREAMING: int
    GET_WIFI_SETUP: int
    PUT_WIFI_SETUP: int
    PUT_STING_START: int
    PUT_STING_STOP: int
    PUT_STING_STATUS: int
    PUT_STING_ALERT: int
    PUT_KEEP_ALIVE: int
    GET_STING_STATUS: int
    PUT_STING_TEST: int
    PUT_RTSP_STREAMING: int
    GET_UOM_URI: int
    GET_UOM: int
    PUT_UOM: int
    GET_AUTH_KEY: int
    PUT_AUTH_KEY: int
    PUT_HEALTH: int
    PUT_TCP_REQUEST: int
    GET_STING_START: int
    GET_LOGS_URI: int
    @staticmethod
    def Name(number: int) -> str: ...
    @staticmethod
    def Value(name: str) -> int: ...

class Response(google.protobuf.message.Message):
    request_id: int
    request_type: int
    status_code: int
    status_message: str
    status: Status
    settings: Settings
    sensor_data: SensorData
    control: Control

class SensorData(google.protobuf.message.Message):
    sensor_type: int
    value: int
    is_alert: bool
    timestamp: int
    value_milli: int

class SensorType:
    SOUND: int
    MOTION: int
    TEMPERATURE: int
    HUMIDITY: int
    LIGHT: int
    NIGHT: int

class Settings(google.protobuf.message.Message):
    FR50HZ: int
    FR60HZ: int
    ANY: int
    FR2_4GHZ: int
    FR5_0GHZ: int
    class SensorSettings(google.protobuf.message.Message): ...
    class StreamSettings(google.protobuf.message.Message): ...
    night_vision: bool
    sensors: Settings.SensorSettings
    streams: Settings.StreamSettings
    volume: int
    anti_flicker: int
    sleep_mode: bool
    status_light_on: bool
    mounting_mode: int
    wifi_band: int
    mic_mute_on: bool

class Status(google.protobuf.message.Message):
    DISCONNECTED: int
    CONNECTED: int
    upgrade_downloaded: bool
    connection_to_server: int
    current_version: str
    mode: int
    is_security_upgrade: bool
    downloaded_version: str
    hardware_version: str

class Stream(google.protobuf.message.Message):
    LOCAL: int
    REMOTE: int
    RTSP: int
    P2P: int
    type: int
    url: str
    bps: int

class StreamIdentifier:
    DVR: int
    ANALYTICS: int
    MOBILE: int
    STAND: int
    TRAVEL: int
    SWITCH: int

class Streaming(google.protobuf.message.Message):
    STARTED: int
    STOPPED: int
    PAUSED: int
    id: int
    status: int
    rtmp_url: str
    attempts: int
