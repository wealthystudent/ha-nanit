"""High-level API for the Nanit Sound & Light Machine."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import ssl
from collections.abc import Callable
from typing import Any

import aiohttp

from aionanit.auth import TokenManager
from aionanit import NanitConnectionError
from aionanit.rest import NanitRestClient

from .exceptions import NanitTransportError
from .models import (
    SoundLightEvent,
    SoundLightEventKind,
    SoundLightFullState,
    SoundLightRoutine,
)
from .sl_protocol import (
    SLDecodedRoutine,
    build_brightness_cmd,
    build_color_cmd,
    build_light_enabled_cmd,
    build_power_cmd,
    build_sl_keepalive,
    build_sound_on_cmd,
    build_track_cmd,
    build_volume_cmd,
    classify_message,
    decode_full_state,
    decode_routines,
    decode_sensors,
)

_LOGGER = logging.getLogger(__name__)

_KEEPALIVE_INTERVAL: float = 25.0
_HEARTBEAT_INTERVAL: float = 60.0
_HANDSHAKE_TIMEOUT: float = 15.0
_INITIAL_BACKOFF: float = 1.85
_BACKOFF_FACTOR: float = 1.618
_MAX_BACKOFF: float = 60.0
_JITTER_MAX: float = 1.0

# Periodic re-poll: reconnect to get fresh state from device.
# The device sends full state on connect, so a reconnect is effectively a poll.
_POLL_INTERVAL: float = 300.0  # seconds between state re-polls (5 minutes)

# Device token refresh: re-fetch every 6 hours (tokens last ~1 week)
_TOKEN_REFRESH_INTERVAL: float = 6 * 3600


class NanitSoundLight:
    """High-level API for a single Nanit Sound & Light Machine.

    Connects via local WebSocket (wss://{ip}:442) using a device token
    obtained from the cloud API (/speakers/{uid}/udtokens).

    Receives push state updates (protobuf) and provides command methods.
    """

    # TODO: com6056's reference implementation uses a cloud relay at
    # wss://remote.nanit.com/speakers/{uid}/user_connect/ with Bearer token
    # instead of a direct local WebSocket. This would eliminate the need for
    # a speaker IP and significantly improve UX. Not implemented yet — it's
    # a different connection model that needs its own design.

    def __init__(
        self,
        speaker_uid: str,
        device_ip: str,
        token_manager: TokenManager,
        rest_client: NanitRestClient,
        session: aiohttp.ClientSession,
    ) -> None:
        self._speaker_uid = speaker_uid
        self._device_ip = device_ip
        self._token_manager = token_manager
        self._rest = rest_client
        self._session = session

        self._state = SoundLightFullState()
        self._subscribers: list[Callable[[SoundLightEvent], None]] = []
        self._connected: bool = False
        self._stopped: bool = False
        self._device_token: str | None = None

        # WebSocket state
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._token_refresh_task: asyncio.Task[None] | None = None
        self._ssl_ctx: ssl.SSLContext | None = None
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def speaker_uid(self) -> str:
        return self._speaker_uid

    @property
    def state(self) -> SoundLightFullState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(
        self, callback: Callable[[SoundLightEvent], None]
    ) -> Callable[[], None]:
        """Subscribe to S&L events. Returns an unsubscribe callable."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _unsubscribe

    def _fire_event(self, kind: SoundLightEventKind) -> None:
        """Fire an event to all subscribers."""
        event = SoundLightEvent(kind=kind, state=self._state)
        for cb in self._subscribers:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in S&L event subscriber")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Start the S&L connection.

        1. Fetch device token from cloud API
        2. Connect WebSocket to local device
        3. Start recv loop, keepalive, and token refresh tasks
        """
        self._stopped = False
        await self._async_fetch_device_token()
        await self._async_connect()

    async def async_stop(self) -> None:
        """Stop the S&L connection gracefully."""
        self._stopped = True
        await self._async_close_ws()

        for task_attr in ("_poll_task", "_token_refresh_task"):
            task = getattr(self, task_attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_attr, None)

        self._connected = False
        self._fire_event(SoundLightEventKind.CONNECTION_CHANGE)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_set_power(self, on: bool) -> None:
        """Turn device power on or off (field 5)."""
        cmd = build_power_cmd(on)
        await self._async_send(cmd)
        self._state = dataclasses.replace(self._state, power_on=on)
        self._fire_event(SoundLightEventKind.STATE_UPDATE)

    async def async_set_light_enabled(self, on: bool) -> None:
        """Turn the night light on or off (field 2 sub-1).

        Passes current color values to preserve them in the command.
        """
        cmd = build_light_enabled_cmd(
            on,
            color_a=self._state.color_r,
            color_b=self._state.color_g,
        )
        await self._async_send(cmd)
        self._state = dataclasses.replace(self._state, light_enabled=on)
        self._fire_event(SoundLightEventKind.STATE_UPDATE)

    async def async_set_sound_on(self, on: bool) -> None:
        """Turn sound on or off (field 4 sub-1).

        Passes current track name to preserve it in the command.
        """
        cmd = build_sound_on_cmd(on, current_track=self._state.current_track)
        await self._async_send(cmd)
        self._state = dataclasses.replace(self._state, sound_on=on)
        self._fire_event(SoundLightEventKind.STATE_UPDATE)

    async def async_set_track(self, track_name: str) -> None:
        """Change the sound track.

        Passes current sound_on state to preserve it.
        """
        cmd = build_track_cmd(track_name, sound_on=self._state.sound_on)
        await self._async_send(cmd)
        self._state = dataclasses.replace(self._state, current_track=track_name)
        self._fire_event(SoundLightEventKind.STATE_UPDATE)

    async def async_set_brightness(self, brightness: float) -> None:
        """Set light brightness (0.0-1.0)."""
        cmd = build_brightness_cmd(brightness)
        await self._async_send(cmd)
        self._state = dataclasses.replace(self._state, brightness=brightness)
        self._fire_event(SoundLightEventKind.STATE_UPDATE)

    async def async_set_volume(self, volume: float) -> None:
        """Set sound volume (0.0-1.0).

        Uses protobuf state field 3 (FIXED32 float). Confirmed working
        by user testing.
        """
        cmd = build_volume_cmd(volume)
        await self._async_send(cmd)
        self._state = dataclasses.replace(self._state, volume=volume)
        self._fire_event(SoundLightEventKind.STATE_UPDATE)

    async def async_set_color(self, color_a: float, color_b: float) -> None:
        """Set light color using the device's 2-parameter color model.

        The exact color model (likely HSV hue+saturation) is experimental.
        color_a maps to state field 2 sub-field 2 (range 0.0-1.0).
        color_b maps to state field 2 sub-field 3 (range 0.0-1.0).

        Passes current light_enabled state to preserve it.
        """
        cmd = build_color_cmd(color_a, color_b, light_enabled=self._state.light_enabled)
        await self._async_send(cmd)
        self._state = dataclasses.replace(
            self._state, color_r=color_a, color_g=color_b
        )
        self._fire_event(SoundLightEventKind.STATE_UPDATE)

    # ------------------------------------------------------------------
    # Internal — token management
    # ------------------------------------------------------------------

    async def _async_fetch_device_token(self) -> None:
        """Fetch the RS256 device token from /speakers/{uid}/udtokens.

        Uses the rest client's method if available, otherwise makes the
        API call directly (for compatibility with aionanit < 1.1).
        """
        access_token = await self._token_manager.async_get_access_token()

        if hasattr(self._rest, "async_get_device_token"):
            self._device_token = await self._rest.async_get_device_token(
                access_token, self._speaker_uid
            )
        else:
            # Inline fallback for aionanit versions without this method
            resp = await self._session.post(
                f"{self._rest._base_url}/speakers/{self._speaker_uid}/udtokens",
                headers={"Authorization": access_token},
            )
            if resp.status == 401:
                from aionanit import NanitAuthError
                raise NanitAuthError("Access token invalid for device token request")
            resp.raise_for_status()
            body = await resp.json()
            self._device_token = body["token"]

        _LOGGER.debug(
            "Fetched device token for speaker %s (len=%d)",
            self._speaker_uid,
            len(self._device_token),
        )

    async def _token_refresh_loop(self) -> None:
        """Periodically refresh the device token."""
        try:
            while not self._stopped:
                await asyncio.sleep(_TOKEN_REFRESH_INTERVAL)
                if self._stopped:
                    return
                try:
                    await self._async_fetch_device_token()
                    _LOGGER.debug("S&L device token refreshed")
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("S&L device token refresh failed: %s", err)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Internal — WebSocket connection
    # ------------------------------------------------------------------

    async def _async_connect(self, *, silent: bool = False) -> None:
        """Connect WebSocket to the local S&L device.

        Args:
            silent: If True, suppress CONNECTION_CHANGE events during the
                    disconnect/reconnect cycle.  Used by the poll loop to
                    avoid briefly marking entities as unavailable.
        """
        async with self._connect_lock:
            await self._async_close_ws()

            if not self._device_token:
                raise NanitConnectionError("No device token available")

            if self._ssl_ctx is None:
                self._ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                self._ssl_ctx.check_hostname = False
                self._ssl_ctx.verify_mode = ssl.CERT_NONE

            url = f"wss://{self._device_ip}:442/"
            headers = {
                "Authorization": f"token {self._device_token}",
                "User-Agent": "NanitLite/1.8.0",
            }

            if not silent:
                self._connected = False
                self._fire_event(SoundLightEventKind.CONNECTION_CHANGE)

            try:
                self._ws = await self._session.ws_connect(
                    url,
                    headers=headers,
                    heartbeat=_HEARTBEAT_INTERVAL,
                    timeout=_HANDSHAKE_TIMEOUT,
                    ssl=self._ssl_ctx,
                )
            except Exception as err:
                raise NanitConnectionError(
                    f"S&L WebSocket connect failed: {err}"
                ) from err

            self._connected = True
            loop = asyncio.get_running_loop()
            self._recv_task = loop.create_task(self._recv_loop())
            self._keepalive_task = loop.create_task(self._keepalive_loop())

            # Start token refresh task if not already running
            if self._token_refresh_task is None or self._token_refresh_task.done():
                self._token_refresh_task = loop.create_task(
                    self._token_refresh_loop()
                )

            # (Re)start poll task — cancel stale one first
            if self._poll_task is not None and not self._poll_task.done():
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
            self._poll_task = loop.create_task(self._poll_loop())

            self._fire_event(SoundLightEventKind.CONNECTION_CHANGE)
            _LOGGER.info(
                "S&L device %s connected at %s:442",
                self._speaker_uid,
                self._device_ip,
            )

    async def _async_close_ws(self) -> None:
        """Close WebSocket and cancel recv/keepalive tasks."""
        for task in (self._recv_task, self._keepalive_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._recv_task = None
        self._keepalive_task = None

        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def _async_send(self, data: bytes) -> None:
        """Send binary data over the WebSocket."""
        if self._ws is None or self._ws.closed:
            raise NanitTransportError("S&L not connected")
        try:
            await self._ws.send_bytes(data)
        except Exception as err:
            raise NanitTransportError(f"S&L send failed: {err}") from err

    # ------------------------------------------------------------------
    # Internal — recv loop and message handling
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """Read binary frames and dispatch to message handler."""
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self._on_message(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    _LOGGER.debug("S&L WebSocket closed by device")
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error("S&L WebSocket error: %s", self._ws.exception())
                    break
        except asyncio.CancelledError:
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("S&L recv loop error: %s", err)

        # Auto-reconnect if not explicitly stopped
        self._connected = False
        self._fire_event(SoundLightEventKind.CONNECTION_CHANGE)
        if not self._stopped:
            asyncio.get_running_loop().create_task(self._reconnect_loop())

    def _on_message(self, data: bytes) -> None:
        """Handle a raw binary protobuf message from the S&L device."""
        msg_type = classify_message(data)

        if msg_type == 0:
            # Full state update
            decoded = decode_full_state(data)
            if decoded is not None:
                self._state = dataclasses.replace(
                    self._state,
                    brightness=decoded.brightness if decoded.brightness is not None else self._state.brightness,
                    light_enabled=decoded.light_enabled if decoded.light_enabled is not None else self._state.light_enabled,
                    color_r=decoded.color_r if decoded.color_r is not None else self._state.color_r,
                    color_g=decoded.color_g if decoded.color_g is not None else self._state.color_g,
                    volume=decoded.volume if decoded.volume is not None else self._state.volume,
                    current_track=decoded.current_track if decoded.current_track is not None else self._state.current_track,
                    sound_on=decoded.sound_on if decoded.sound_on is not None else self._state.sound_on,
                    power_on=decoded.power_on if decoded.power_on is not None else self._state.power_on,
                    available_tracks=tuple(decoded.available_tracks) if decoded.available_tracks else self._state.available_tracks,
                    temperature_c=decoded.temperature_c if decoded.temperature_c is not None else self._state.temperature_c,
                    humidity_pct=decoded.humidity_pct if decoded.humidity_pct is not None else self._state.humidity_pct,
                    timezone_rule=decoded.timezone_rule if decoded.timezone_rule is not None else self._state.timezone_rule,
                )
                self._fire_event(SoundLightEventKind.STATE_UPDATE)
                _LOGGER.debug(
                    "S&L state: brightness=%.2f volume=%.2f track=%s power=%s light=%s sound=%s temp=%.1f hum=%.1f",
                    self._state.brightness or 0,
                    self._state.volume or 0,
                    self._state.current_track,
                    self._state.power_on,
                    self._state.light_enabled,
                    self._state.sound_on,
                    self._state.temperature_c or 0,
                    self._state.humidity_pct or 0,
                )

        elif msg_type == 1:
            # Sensor update
            decoded_sensors = decode_sensors(data)
            if decoded_sensors is not None:
                self._state = dataclasses.replace(
                    self._state,
                    temperature_c=decoded_sensors.temperature_c if decoded_sensors.temperature_c is not None else self._state.temperature_c,
                    humidity_pct=decoded_sensors.humidity_pct if decoded_sensors.humidity_pct is not None else self._state.humidity_pct,
                )
                self._fire_event(SoundLightEventKind.SENSOR_UPDATE)

        elif msg_type in (2, 3):
            # Routines update
            decoded_routines = decode_routines(data)
            if decoded_routines:
                new_routines = tuple(
                    SoundLightRoutine(
                        name=r.name,
                        sound_name=r.sound_name,
                        volume=r.volume,
                        brightness=r.brightness,
                    )
                    for r in decoded_routines
                )
                # Merge with existing routines (type 2 and 3 are different sets)
                existing = {r.name: r for r in self._state.routines}
                for r in new_routines:
                    existing[r.name] = r
                self._state = dataclasses.replace(
                    self._state,
                    routines=tuple(existing.values()),
                )
                self._fire_event(SoundLightEventKind.ROUTINES_UPDATE)

        else:
            _LOGGER.debug(
                "S&L unknown message type (len=%d): %s",
                len(data),
                data.hex()[:100],
            )

    # ------------------------------------------------------------------
    # Internal — keepalive and reconnect
    # ------------------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        """Send keepalive messages periodically."""
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL)
                if self._ws is None or self._ws.closed:
                    break
                try:
                    await self._async_send(build_sl_keepalive())
                except NanitTransportError:
                    _LOGGER.warning("S&L keepalive failed, triggering reconnect")
                    break
        except asyncio.CancelledError:
            return

    async def _poll_loop(self) -> None:
        """Periodically reconnect to refresh state from the device.

        The S&L device sends a full state dump on each WebSocket connect.
        By reconnecting every _POLL_INTERVAL seconds we pick up any changes
        made via the Nanit app (which communicates through the cloud, not
        the local WebSocket).
        """
        try:
            while not self._stopped:
                await asyncio.sleep(_POLL_INTERVAL)
                if self._stopped:
                    return
                _LOGGER.debug("S&L poll: reconnecting to refresh state")
                try:
                    await self._async_connect(silent=True)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("S&L poll reconnect failed: %s", err)
        except asyncio.CancelledError:
            return

    async def _reconnect_loop(self) -> None:
        """Exponential-backoff reconnect loop."""
        if self._stopped:
            return

        import random

        backoff = _INITIAL_BACKOFF
        jitter = random.random() * _JITTER_MAX

        while not self._stopped:
            await self._async_close_ws()

            wait_time = backoff + jitter
            jitter = 0.0
            _LOGGER.info("S&L reconnecting in %.1fs", wait_time)
            await asyncio.sleep(wait_time)

            if self._stopped:
                return

            try:
                # Refresh device token before reconnecting
                try:
                    await self._async_fetch_device_token()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Token refresh failed during reconnect, using cached")

                await self._async_connect()
                _LOGGER.info("S&L reconnected successfully")
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("S&L reconnect failed: %s", err)
                self._connected = False
                self._fire_event(SoundLightEventKind.CONNECTION_CHANGE)
                backoff = min(backoff * _BACKOFF_FACTOR, _MAX_BACKOFF)
