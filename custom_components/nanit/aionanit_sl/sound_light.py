"""High-level API for the Nanit Sound & Light Machine.

`NanitSoundLight` keeps its original public surface (constructor, properties,
`subscribe`, `async_start`/`async_stop`, and the `async_set_*` command
methods) but is now a facade over the transport ported from
com6056/nanit-sound-light (`transport.py`), which replaces the old
fire-and-forget socket with the validated model: one command in flight with
await-ack and no re-send, a backend readiness gate, dual local+remote
sockets with app-matching backoff, and combined `Settings` writes.

The facade adds the command layer that was validated in production there:

- **Coalescing**: commands arriving within a short window (a Home Assistant
  scene touching power + sound + volume + light at once) are merged and sent
  as ONE combined `Settings` message instead of racing per-field writes.
- **Pin-guard**: after a command, the affected fields are pinned so a stale
  device echo can't flap a just-commanded value back. A pin releases early
  the moment the device confirms the value.
- **Optimistic state + rollback**: commands update the published state
  immediately, and a failed send rolls that back so entities never show a
  state the device didn't accept.
- **Validated light semantics** (device firmware 1.3.1, 2026-07-11): the
  lamp emits iff `isOn && brightness > 0 && !noColor`. Light OFF is sent as
  `brightness: 0` (round-trips the stored color, unlike the app's `noColor`
  off which relights in white), and light ON always sends explicit
  hue/saturation, the only reliable color restore.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import struct
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from aionanit.auth import TokenManager
from aionanit.rest import NanitRestClient

from .exceptions import NanitTransportError
from .models import (
    SoundLightEvent,
    SoundLightEventKind,
    SoundLightFullState,
)
from .transport import SoundLightTransport

_LOGGER = logging.getLogger(__name__)

# How long to gather rapid-fire commands before flushing them as one combined
# message. A HA scene fires its member entities within the same event-loop
# tick, so a short window collapses power + sound + volume + light into one
# write.
COMMAND_COALESCE_DELAY = 0.15  # seconds

# After a command we "pin" the fields it set so a stale device echo can't flap
# them back. The device's ACK is fast but its REPORTED STATE lags badly: it
# keeps reporting the pre-command value for up to ~15s before catching up. A
# pin is released early the moment the device confirms our value, so the
# normal case stays snappy. This window is only the safety cap for that slow
# propagation.
COMMAND_PIN_SECONDS = 30.0

# This is a push client: real-time state arrives over the websocket. The
# periodic poll is a backup nudge (a light GetSettings, NOT a reconnect) that
# also reconciles optimistic state and re-establishes dropped sockets.
_POLL_INTERVAL = 30.0  # seconds
# After a poll ping, give the device this long to answer before reading state.
_POLL_SETTLE = 2.0  # seconds

# On startup, wait briefly for the device's first real state so entities
# don't start "unknown". Capped so an unreachable device can't stall setup.
_INITIAL_STATE_ATTEMPTS = 6  # x interval = ~3s max
_INITIAL_STATE_INTERVAL = 0.5  # seconds

_NO_SOUND = "No sound"


def _proto_float32(value: Any) -> Any:
    """Round a float through protobuf's float32 wire precision.

    Settings.brightness/volume and Color.hue/saturation are proto2 `float`
    (32-bit). We command a Python float64, but the device's echo comes back
    float32-rounded, so pinning the raw float64 would make the confirmation
    equality fail for almost every real value and the pin would silently hold
    the full window. Pinning the float32-rounded value keeps the comparison
    EXACT (no tolerance, so no false confirmation) while matching what the
    device can actually echo back.
    """
    if isinstance(value, float):
        return struct.unpack("<f", struct.pack("<f", value))[0]
    return value


def _command_to_device_fields(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Map a control command's kwargs to the device-state keys it affects."""
    fields: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key == "sound":
            if value is not None:
                fields["current_sound"] = value
        elif key in ("is_on", "brightness", "volume"):
            fields[key] = value
        elif key == "color":
            if "noColor" in value:
                fields["no_color"] = value["noColor"]
            if "hue" in value:
                fields["hue"] = value["hue"]
            if "saturation" in value:
                fields["saturation"] = value["saturation"]
            if "brightness" in value:
                fields["brightness"] = value["brightness"]
    return fields


def _has_usable_state(state: dict[str, Any]) -> bool:
    """True once the device has reported real state (not just defaults)."""
    keys = ("brightness", "volume", "current_sound", "hue", "is_on")
    return bool(state) and any(state.get(k) is not None for k in keys)


class NanitSoundLight:
    """High-level API for a single Nanit Sound & Light Machine.

    Connects via the cloud relay
    (wss://remote.nanit.com/speakers/{uid}/user_connect/) and, when the
    speaker is reachable on the LAN, ALSO via a direct local WebSocket
    (wss://{ip}:442, device token auth). Both sockets can be open at once
    and sends prefer local. The local address comes from mDNS discovery
    (via the injected resolver), with the manually configured speaker IP as
    an optional override.

    Receives push state updates (protobuf) and provides command methods.
    """

    def __init__(
        self,
        speaker_uid: str,
        token_manager: TokenManager,
        rest_client: NanitRestClient,
        session: aiohttp.ClientSession,
        device_ip: str | None = None,
        local_host_resolver: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        """Initialize the Sound & Light facade for one speaker."""
        self._speaker_uid = speaker_uid
        self._device_ip = device_ip
        self._token_manager = token_manager
        self._rest = rest_client
        self._session = session

        self._state = SoundLightFullState()
        self._subscribers: list[Callable[[SoundLightEvent], None]] = []
        self._stopped: bool = True
        self._last_connected: bool = False

        # Facade-side merged device state (the transport's parsed fields plus
        # optimistic command overlays), keyed by the transport's field names.
        self._device_view: dict[str, Any] = {}

        # Command coalescing + pin-guard + rollback (see module docstring).
        self._pending_commands: dict[str, Any] = {}
        self._flush_handle: asyncio.TimerHandle | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._pinned_fields: dict[str, tuple[Any, float]] = {}
        self._rollback_snapshot: dict[str, Any] = {}

        # Last-known-good values for restores: light ON must re-send explicit
        # hue/saturation (the device does not restore color on a bare
        # re-enable), and a turn-on from brightness 0 needs a real brightness.
        self._last_color: dict[str, float] | None = None
        self._last_track: str | None = None
        self._last_brightness: float | None = None

        self._poll_task: asyncio.Task[None] | None = None

        async def _access_token() -> str:
            return await token_manager.async_get_access_token()

        async def _device_token(uid: str) -> str:
            access = await token_manager.async_get_access_token()
            return await rest_client.async_get_device_token(access, uid)

        self._api = SoundLightTransport(
            access_token_provider=_access_token,
            device_token_fetcher=_device_token,
        )
        if local_host_resolver is not None:
            self._api.set_local_host_resolver(local_host_resolver)
        self._api.set_state_change_callback(self._on_push)
        self._api.set_connection_change_callback(self._on_connection_change)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def speaker_uid(self) -> str:
        """Return the speaker's uid."""
        return self._speaker_uid

    @property
    def state(self) -> SoundLightFullState:
        """Return the last published device state."""
        return self._state

    @property
    def connected(self) -> bool:
        """True when the device is reachable AND attached.

        A cloud-relay socket can be up while the physical device is still
        detached behind it, in which case commands only stall. So both a
        live socket and the (sticky) attachment latch are required.
        """
        return self._api.is_websocket_connected(self._speaker_uid) and self._api.is_device_attached(
            self._speaker_uid
        )

    def restore_state(self, state: SoundLightFullState) -> None:
        """Restore persisted state (used by coordinator on startup)."""
        self._state = state
        if state.color_r is not None and state.color_g is not None:
            self._last_color = {
                "hue": state.color_r,
                "saturation": state.color_g,
                "brightness": state.brightness or 1.0,
            }
        if state.current_track and state.current_track != _NO_SOUND:
            self._last_track = state.current_track
        if state.brightness:
            self._last_brightness = state.brightness

    @property
    def connection_mode(self) -> str:
        """Return the current connection mode: 'local', 'cloud', or 'unavailable'."""
        mode = self._api.active_transport(self._speaker_uid)
        return mode if mode is not None else "unavailable"

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[SoundLightEvent], None]) -> Callable[[], None]:
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
            except Exception:
                _LOGGER.exception("Error in S&L event subscriber")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Start the S&L connection.

        Opens the cloud relay socket (and the direct local socket when the
        speaker is discoverable/configured on the LAN), primes the device
        state with an initial GetSettings, and starts the 30s backup poll.
        Connection failures are not fatal: the transport reconnects with
        backoff and the poll re-establishes dropped sockets, while entities
        show unavailable until the device is reachable.
        """
        self._stopped = False
        self._api.register_device(self._speaker_uid, self._device_ip)
        try:
            await self._api.connect_device(self._speaker_uid)
        except Exception as err:
            _LOGGER.warning(
                "S&L %s initial connection failed; will retry in background: %s",
                self._speaker_uid,
                err,
            )

        try:
            await self._api.send_saved_sounds_request(self._speaker_uid)
            await self._api.send_ping_for_state(self._speaker_uid)
            for _ in range(_INITIAL_STATE_ATTEMPTS):
                if _has_usable_state(self._api.get_device_state(self._speaker_uid)):
                    break
                await asyncio.sleep(_INITIAL_STATE_INTERVAL)
        except Exception as err:
            _LOGGER.debug("S&L %s initial state prime failed: %s", self._speaker_uid, err)

        self._ingest_device_state()
        self._on_connection_change(self._speaker_uid)
        self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())

    async def async_stop(self) -> None:
        """Stop the S&L connection gracefully."""
        self._stopped = True

        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._pending_commands.clear()
        self._pinned_fields.clear()
        self._rollback_snapshot.clear()

        for task_attr in ("_poll_task", "_flush_task"):
            task: asyncio.Task[None] | None = getattr(self, task_attr)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            setattr(self, task_attr, None)

        await self._api.close()

        self._last_connected = False
        self._fire_event(SoundLightEventKind.CONNECTION_CHANGE)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_set_power(self, on: bool) -> None:
        """Turn device power on or off (a bare `Settings{isOn}`)."""
        self._queue_command({"is_on": on})

    async def async_set_light_enabled(self, on: bool) -> None:
        """Turn the night light on or off (validated semantics).

        OFF sends `brightness: 0`: an app-legitimate off state that
        round-trips the device's stored color, unlike the app's own
        `noColor` off (a bare re-enable from that lands on white).

        ON powers the device on, restores a non-zero brightness when the
        light was dimmed to zero, and ALWAYS sends explicit hue/saturation
        (the device does not restore its previous color by itself). When the
        device was fully off, sound is explicitly kept at "No sound" so
        turning the light on can't unexpectedly resume audio.
        """
        if not on:
            self._queue_command({"brightness": 0.0})
            return

        view = self._device_view
        kwargs: dict[str, Any] = {"is_on": True}
        if not view.get("is_on"):
            kwargs["sound"] = _NO_SOUND
        brightness = view.get("brightness")
        if not brightness or brightness <= 0:
            kwargs["brightness"] = self._last_brightness or 1.0
        last = self._last_color or {}
        kwargs["color"] = {
            "noColor": False,
            "hue": last.get("hue", view.get("hue", 0.0)),
            "saturation": last.get("saturation", view.get("saturation", 0.0)),
        }
        self._queue_command(kwargs)

    async def async_set_sound_on(self, on: bool) -> None:
        """Turn sound on or off.

        ON resumes the last known track (or lets the device pick its last
        track when none is known yet); OFF selects "No sound".
        """
        if on:
            self._queue_command({"sound": self._last_track})
        else:
            self._queue_command({"sound": _NO_SOUND})

    async def async_set_track(self, track_name: str) -> None:
        """Change the sound track (selecting a track starts playing it)."""
        if track_name != _NO_SOUND:
            self._last_track = track_name
        self._queue_command({"sound": track_name})

    async def async_set_brightness(self, brightness: float) -> None:
        """Set light brightness (0.0-1.0)."""
        if brightness > 0:
            self._last_brightness = brightness
        self._queue_command({"brightness": brightness})

    async def async_set_volume(self, volume: float) -> None:
        """Set sound volume (0.0-1.0)."""
        self._queue_command({"volume": volume})

    async def async_set_color(self, color_a: float, color_b: float) -> None:
        """Set light color (hue and saturation, both 0.0-1.0)."""
        self._last_color = {
            "hue": color_a,
            "saturation": color_b,
            "brightness": self._device_view.get("brightness") or 1.0,
        }
        self._queue_command({"color": {"noColor": False, "hue": color_a, "saturation": color_b}})

    # ------------------------------------------------------------------
    # Internal: command coalescing, pins, rollback
    # ------------------------------------------------------------------

    def _queue_command(self, kwargs: dict[str, Any]) -> None:
        """Queue a control command, coalescing concurrent fields into one send.

        Entity services (switch/light/select/number) call this, and a scene
        calls several at once. Rather than sending a racing message per
        field, we merge the fields, apply optimistic state for instant UI
        feedback, and schedule a single combined flush.
        """
        if self._stopped:
            raise NanitTransportError(
                f"S&L {self._speaker_uid} is not started, cannot send commands"
            )

        _LOGGER.debug("Queuing command for %s: %s", self._speaker_uid, kwargs)
        self._pending_commands.update(kwargs)
        self._apply_optimistic_state(kwargs)

        loop = asyncio.get_running_loop()
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        self._flush_handle = loop.call_later(COMMAND_COALESCE_DELAY, self._start_flush)

    def _start_flush(self) -> None:
        """Timer callback: launch the flush task (strong ref against GC)."""
        self._flush_handle = None
        self._flush_task = asyncio.get_event_loop().create_task(self._flush_commands())

    async def _flush_commands(self) -> None:
        """Send all coalesced fields as one combined command."""
        kwargs = dict(self._pending_commands)
        self._pending_commands.clear()
        if not kwargs:
            return

        try:
            await self._api.send_control_command(self._speaker_uid, **kwargs)
            # The command's ack already confirms receipt, and the device
            # pushes the resulting state on its own; the 30s poll reconciles.
            self._rollback_snapshot.clear()
        except Exception as err:
            _LOGGER.error(
                "Control command failed for %s (%s): %s, rolling back",
                self._speaker_uid,
                type(err).__name__,
                err,
            )
            self._rollback_optimistic_state()

    def _apply_optimistic_state(self, kwargs: dict[str, Any]) -> None:
        """Apply a command's fields to the published state immediately.

        Also pins each field (so a stale echo can't flap it back) and
        snapshots the prior value (so a failed send can be rolled back).
        """
        fields = _command_to_device_fields(kwargs)
        expiry = asyncio.get_running_loop().time() + COMMAND_PIN_SECONDS

        for key, value in fields.items():
            if key not in self._rollback_snapshot:
                self._rollback_snapshot[key] = self._device_view.get(key)
            self._device_view[key] = value
            # Pin the float32-rounded value so the device's echo (float32 on
            # the wire) compares equal and releases the pin early.
            self._pinned_fields[key] = (_proto_float32(value), expiry)

        self._publish()

    def _rollback_optimistic_state(self) -> None:
        """Undo optimistic state after a failed send so the UI doesn't lie."""
        self._pinned_fields.clear()
        snapshot = self._rollback_snapshot
        self._rollback_snapshot = {}
        if not snapshot:
            return
        for key, value in snapshot.items():
            if value is None:
                self._device_view.pop(key, None)
            else:
                self._device_view[key] = value
        self._publish()
        _LOGGER.warning(
            "Rolled back optimistic state for %s after a failed command",
            self._speaker_uid,
        )

    # ------------------------------------------------------------------
    # Internal: inbound state
    # ------------------------------------------------------------------

    def _merge_parsed_state(self, parsed: dict[str, Any]) -> None:
        """Merge parsed device state into the view, honoring active pins.

        A pinned field is suppressed only while the pin is active AND the
        incoming value contradicts what we commanded. If the device confirms
        our value (or the window lapses) the pin is released so normal
        updates (and genuine external changes) flow again.
        """
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            now = float("inf")  # no loop (shutdown): let pins lapse
        for key, value in parsed.items():
            pin = self._pinned_fields.get(key)
            if pin is not None:
                pinned_value, expiry = pin
                if now >= expiry or value == pinned_value:
                    self._pinned_fields.pop(key, None)
                else:
                    continue  # stale/contradicting echo within window: suppress
            self._device_view[key] = value

    def _ingest_device_state(self) -> None:
        """Pull the transport's parsed state, merge it, and publish."""
        parsed = dict(self._api.get_device_state(self._speaker_uid))
        if not parsed:
            return

        # Remember the last real color/track/brightness the device reported,
        # for the light-on color restore and sound-on track resume.
        if (
            not parsed.get("no_color", False)
            and parsed.get("hue") is not None
            and parsed.get("saturation") is not None
        ):
            self._last_color = {
                "hue": parsed["hue"],
                "saturation": parsed["saturation"],
                "brightness": parsed.get("brightness") or 1.0,
            }
        track = parsed.get("current_sound")
        if track and track != _NO_SOUND:
            self._last_track = track
        if parsed.get("brightness"):
            self._last_brightness = parsed["brightness"]

        self._merge_parsed_state(parsed)
        self._publish()

    def _mapped_updates(self) -> dict[str, Any]:
        """Map the merged device view onto SoundLightFullState field updates."""
        view = self._device_view
        updates: dict[str, Any] = {}
        if "brightness" in view:
            updates["brightness"] = view["brightness"]
        if "volume" in view:
            updates["volume"] = view["volume"]
        if "is_on" in view:
            updates["power_on"] = view["is_on"]
        if "hue" in view:
            updates["color_r"] = view["hue"]
        if "saturation" in view:
            updates["color_g"] = view["saturation"]
        if "current_sound" in view:
            current = view["current_sound"]
            if current == _NO_SOUND:
                updates["sound_on"] = False
            elif current is not None:
                updates["sound_on"] = True
                updates["current_track"] = current
        if "available_sounds" in view:
            updates["available_tracks"] = tuple(
                t for t in view["available_sounds"] if t != _NO_SOUND
            )
        if "temperature" in view:
            updates["temperature_c"] = view["temperature"]
        if "humidity" in view:
            updates["humidity_pct"] = view["humidity"]
        # The lamp emits iff isOn && brightness > 0 && !noColor (validated
        # on-device). Only computed once power and brightness are both known,
        # so a restored value isn't clobbered by a partial first frame.
        if "is_on" in view and "brightness" in view:
            updates["light_enabled"] = bool(
                view["is_on"]
                and (view["brightness"] or 0.0) > 0
                and not view.get("no_color", False)
            )
        return updates

    def _publish(self) -> None:
        """Publish the merged view as SoundLightFullState and notify."""
        updates = self._mapped_updates()
        if not updates:
            return
        new_state = dataclasses.replace(self._state, **updates)
        if new_state == self._state:
            return
        self._state = new_state
        self._fire_event(SoundLightEventKind.STATE_UPDATE)

    async def _on_push(self, _speaker_uid: str) -> None:
        """Handle a real-time push (external change) from the transport."""
        self._ingest_device_state()

    def _on_connection_change(self, _speaker_uid: str) -> None:
        """Re-derive connectivity and notify subscribers on a change."""
        connected = self.connected
        if connected == self._last_connected:
            return
        self._last_connected = connected
        _LOGGER.debug(
            "S&L %s connection changed: connected=%s (mode=%s)",
            self._speaker_uid,
            connected,
            self.connection_mode,
        )
        self._fire_event(SoundLightEventKind.CONNECTION_CHANGE)

    # ------------------------------------------------------------------
    # Internal: poll
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Light backup poll: nudge the device for state every 30s.

        Real-time state arrives via push; this reconciles optimistic state,
        keeps temperature/humidity fresh, and (via the transport) re-opens
        dropped sockets. It does NOT tear the connection down.
        """
        try:
            while not self._stopped:
                await asyncio.sleep(_POLL_INTERVAL)
                if self._stopped:
                    return
                try:
                    await self._api.send_ping_for_state(self._speaker_uid)
                    await asyncio.sleep(_POLL_SETTLE)
                    self._ingest_device_state()
                    self._on_connection_change(self._speaker_uid)
                except Exception as err:
                    _LOGGER.debug("S&L %s poll failed: %s", self._speaker_uid, err)
        except asyncio.CancelledError:
            return
