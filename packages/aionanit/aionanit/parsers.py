from __future__ import annotations

from .models import ControlState, NightLightState, SensorState, SettingsState, StatusState
from .proto import (
    ControlNightLight,
    MountingMode,
    Response,
    SensorData,
    SettingsWifiBand,
    StatusConnectionToServer,
)

_WIFI_BAND_MAP: dict[int, str] = {
    SettingsWifiBand.ANY: "any",
    SettingsWifiBand.FR2_4GHZ: "2.4ghz",
    SettingsWifiBand.FR5_0GHZ: "5ghz",
}

_MOUNTING_MODE_MAP: dict[int, str] = {
    MountingMode.STAND: "stand",
    MountingMode.TRAVEL: "travel",
    MountingMode.SWITCH: "switch",
}


def _parse_sensor_data(
    sensor_data_list: list[SensorData],
    current: SensorState,
) -> SensorState:
    from .proto import SensorType as ProtoSensorType

    temperature = current.temperature
    humidity = current.humidity
    light = current.light
    sound_alert = current.sound_alert
    motion_alert = current.motion_alert
    night = current.night

    for sd in sensor_data_list:
        if sd.sensor_type == ProtoSensorType.TEMPERATURE:
            if sd.value_milli:
                temperature = sd.value_milli / 1000.0
            elif sd.value:
                temperature = float(sd.value)
        elif sd.sensor_type == ProtoSensorType.HUMIDITY:
            if sd.value_milli:
                humidity = sd.value_milli / 1000.0
            elif sd.value:
                humidity = float(sd.value)
        elif sd.sensor_type == ProtoSensorType.LIGHT:
            light = sd.value
        elif sd.sensor_type == ProtoSensorType.SOUND:
            sound_alert = sd.is_alert
        elif sd.sensor_type == ProtoSensorType.MOTION:
            motion_alert = sd.is_alert
        elif sd.sensor_type == ProtoSensorType.NIGHT:
            night = bool(sd.value)

    return SensorState(
        temperature=temperature,
        humidity=humidity,
        light=light,
        sound_alert=sound_alert,
        motion_alert=motion_alert,
        night=night,
    )


def _parse_status(resp: Response) -> StatusState:
    if resp.HasField("status"):
        return _parse_status_from_proto(resp.status)
    return StatusState()


def _parse_status_from_proto(status: object) -> StatusState:
    from .proto import Status as ProtoStatus

    if not isinstance(status, ProtoStatus):
        return StatusState()

    connected: bool | None = None
    if status.HasField("connection_to_server"):
        connected = status.connection_to_server == StatusConnectionToServer.CONNECTED

    return StatusState(
        connected_to_server=connected,
        firmware_version=status.current_version or None,
        hardware_version=status.hardware_version or None,
        mounting_mode=_MOUNTING_MODE_MAP.get(status.mode),
    )


def _parse_settings(resp: Response) -> SettingsState:
    if resp.HasField("settings"):
        return _parse_settings_from_proto(resp.settings)
    return SettingsState()


def _parse_settings_from_proto(settings: object) -> SettingsState:
    from .proto import Settings as ProtoSettings

    if not isinstance(settings, ProtoSettings):
        return SettingsState()

    return SettingsState(
        night_vision=settings.night_vision if settings.HasField("night_vision") else None,
        volume=settings.volume if settings.HasField("volume") else None,
        sleep_mode=settings.sleep_mode if settings.HasField("sleep_mode") else None,
        status_light_on=settings.status_light_on if settings.HasField("status_light_on") else None,
        mic_mute_on=settings.mic_mute_on if settings.HasField("mic_mute_on") else None,
        wifi_band=_WIFI_BAND_MAP.get(settings.wifi_band)
        if settings.HasField("wifi_band")
        else None,
        mounting_mode=_MOUNTING_MODE_MAP.get(settings.mounting_mode)
        if settings.HasField("mounting_mode")
        else None,
        night_light_brightness=settings.night_light_brightness
        if settings.HasField("night_light_brightness")
        else None,
    )


def _parse_control(resp: Response) -> ControlState:
    if resp.HasField("control"):
        return _parse_control_from_proto(resp.control)
    return ControlState()


def _parse_control_from_proto(control: object) -> ControlState:
    from .proto import Control as ProtoControl

    if not isinstance(control, ProtoControl):
        return ControlState()

    night_light: NightLightState | None = None
    if control.HasField("night_light"):
        if control.night_light == ControlNightLight.LIGHT_ON:
            night_light = NightLightState.ON
        else:
            night_light = NightLightState.OFF

    sensor_transfer_enabled: bool | None = None
    if control.HasField("sensor_data_transfer"):
        sdt = control.sensor_data_transfer
        sensor_transfer_enabled = any(
            [sdt.sound, sdt.motion, sdt.temperature, sdt.humidity, sdt.light, sdt.night]
        )

    return ControlState(
        night_light=night_light,
        night_light_timeout=control.night_light_timeout
        if control.HasField("night_light_timeout")
        else None,
        sensor_data_transfer_enabled=sensor_transfer_enabled,
    )
