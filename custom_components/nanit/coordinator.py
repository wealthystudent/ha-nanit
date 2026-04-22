"""Coordinators for the Nanit integration.

NanitPushCoordinator: Push-based coordinator that wraps NanitCamera.subscribe().
    Fires async_set_updated_data on every CameraEvent callback (sensor, settings,
    control, status, connection changes). No polling — all data arrives via
    WebSocket push.

    Entity availability uses a grace period so that brief reconnections (e.g.,
    pre-emptive token refresh) do not surface as "Unavailable" in HA.

NanitCloudCoordinator: Polls the Nanit cloud API for motion/sound events every
    CLOUD_POLL_INTERVAL seconds.

NanitSoundLightCoordinator: Push-based coordinator wrapping NanitSoundLight.subscribe().
    Receives state updates from the S&L device's local WebSocket.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import dataclasses

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from aionanit import NanitAuthError, NanitCamera, NanitConnectionError
from aionanit.models import Baby, CameraEvent, CameraState, CloudEvent

from .aionanit_sl.models import SoundLightEvent, SoundLightEventKind, SoundLightFullState
from .aionanit_sl.sound_light import NanitSoundLight

from .const import CLOUD_POLL_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from . import NanitConfigEntry
    from .hub import NanitHub

_LOGGER = logging.getLogger(__name__)

# How long to wait before marking entities unavailable after a disconnect.
# If the WebSocket reconnects within this window, entities never go unavailable.
_AVAILABILITY_GRACE_SECONDS: float = 30.0


class NanitPushCoordinator(DataUpdateCoordinator[CameraState]):
    """Push-based coordinator that receives state updates from NanitCamera.subscribe().

    No polling is configured — async_set_updated_data() is called by the camera
    callback on every state change. Entity availability is driven by the
    ``connected`` flag which tracks the WebSocket connection state, debounced
    by a grace period so brief reconnections don't flash "Unavailable".
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        camera: NanitCamera,
        baby: Baby,
    ) -> None:
        """Initialize the push coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{camera.uid}",
        )
        self.camera = camera
        self.baby = baby
        self.connected: bool = False
        self._unsubscribe: Callable[[], None] | None = None
        self._availability_timer: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Start the camera and subscribe to push events.

        Called once from async_setup_entry after the coordinator is created.
        """
        self._unsubscribe = self.camera.subscribe(self._on_camera_event)
        await self.camera.async_start()
        self.connected = self.camera.connected
        self.async_set_updated_data(self.camera.state)

    @callback
    def _on_camera_event(self, event: CameraEvent) -> None:
        """Handle a push event from NanitCamera.subscribe()."""
        transport_connected = self.camera.connected

        if transport_connected:
            # Connection is up — cancel any pending unavailability timer
            # and mark connected immediately.
            self._cancel_availability_timer()
            if not self.connected:
                _LOGGER.info("Camera %s reconnected", self.camera.uid)
            self.connected = True
        elif self.connected:
            # Connection just dropped — start the grace period.
            # Don't mark unavailable yet; give the transport time to reconnect.
            _LOGGER.debug(
                "Camera %s disconnected (grace period %.0fs): %s",
                self.camera.uid,
                _AVAILABILITY_GRACE_SECONDS,
                event.state.connection.last_error,
            )
            self._start_availability_timer()
        # If already disconnected (self.connected is False) and transport is
        # still disconnected, do nothing — timer is already running or fired.

        self.async_set_updated_data(event.state)

    @callback
    def _on_availability_timeout(self, _now: object) -> None:
        """Grace period expired — mark entities unavailable."""
        self._availability_timer = None
        if not self.camera.connected:
            _LOGGER.warning(
                "Camera %s still disconnected after %.0fs grace period",
                self.camera.uid,
                _AVAILABILITY_GRACE_SECONDS,
            )
            self.connected = False
            self.async_update_listeners()

    def _start_availability_timer(self) -> None:
        """Start (or restart) the grace period timer."""
        self._cancel_availability_timer()
        self._availability_timer = async_call_later(
            self.hass, _AVAILABILITY_GRACE_SECONDS, self._on_availability_timeout
        )

    def _cancel_availability_timer(self) -> None:
        """Cancel the grace period timer if running."""
        if self._availability_timer is not None:
            self._availability_timer()
            self._availability_timer = None

    async def async_shutdown(self) -> None:
        """Stop the camera and unsubscribe."""
        self._cancel_availability_timer()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.camera.async_stop()
        await super().async_shutdown()


class NanitCloudCoordinator(DataUpdateCoordinator[list[CloudEvent]]):
    """Polling coordinator for Nanit cloud motion/sound events.

    Polls GET /babies/{uid}/messages every CLOUD_POLL_INTERVAL seconds.
    Entities check event timestamps against a window to determine on/off state.
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        hub: NanitHub,
        baby: Baby,
    ) -> None:
        """Initialize the cloud coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{baby.uid}_cloud",
            update_interval=timedelta(seconds=CLOUD_POLL_INTERVAL),
        )
        self._hub = hub
        self.baby = baby

    async def _async_update_data(self) -> list[CloudEvent]:
        """Fetch cloud events from the Nanit API."""
        try:
            client = self._hub.client
            assert client.token_manager is not None
            token = await client.token_manager.async_get_access_token()
            events: list[CloudEvent] = await client.rest_client.async_get_events(
                token, self.baby.uid
            )
            return events
        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except NanitConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cloud_fetch_failed",
                translation_placeholders={"error": str(err)},
            ) from err


_SL_STORE_VERSION = 1
# Fields from SoundLightFullState that we persist across restarts.
_SL_PERSIST_FIELDS = (
    "brightness", "light_enabled", "color_r", "color_g",
    "sound_on", "current_track", "volume",
    "power_on", "temperature_c", "humidity_pct",
)


class NanitSoundLightCoordinator(DataUpdateCoordinator[SoundLightFullState]):
    """Push-based coordinator for the Nanit Sound & Light Machine.

    Wraps NanitSoundLight.subscribe() — receives state updates from
    the S&L device via WebSocket (cloud relay or local).
    No polling — all state is pushed by the device.

    Persists the last known state to HA storage so that entities show
    their previous values on restart (instead of "unknown") until the
    first live update arrives from the device.

    Uses a grace period for disconnections so brief reconnections
    (e.g. during hourly access-token refresh) do not flash entities
    as "Unavailable" in HA.
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        sound_light: NanitSoundLight,
    ) -> None:
        """Initialize the Sound & Light coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{sound_light.speaker_uid}_sound_light",
        )
        self.sound_light = sound_light
        self.baby: Baby = None  # type: ignore[assignment]  # Set by hub._setup_camera before use
        self._unsubscribe: Callable[[], None] | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass,
            _SL_STORE_VERSION,
            f"{DOMAIN}_sl_state_{sound_light.speaker_uid}",
        )
        self._sl_connected: bool = False
        self._availability_timer: CALLBACK_TYPE | None = None
        self._save_timer: CALLBACK_TYPE | None = None
        self._pending_save_state: SoundLightFullState | None = None

    @property
    def connected(self) -> bool:
        """Return debounced connection state (survives brief reconnections)."""
        return self._sl_connected

    async def async_setup(self) -> None:
        """Start the S&L device and subscribe to push events."""
        self._unsubscribe = self.sound_light.subscribe(self._on_sl_event)
        await self.sound_light.async_start()
        self._sl_connected = self.sound_light.connected

        # If the device hasn't sent initial state yet (cloud relay),
        # restore the last known state from disk so entities aren't "unknown".
        state = self.sound_light.state
        if state.power_on is None:
            restored = await self._async_restore_state()
            if restored is not None:
                # Feed restored state into the sound_light instance so
                # entities and coordinator data are consistent.
                self.sound_light.restore_state(restored)
                state = restored
                _LOGGER.debug(
                    "S&L %s: restored saved state (power=%s, track=%s, vol=%s)",
                    self.sound_light.speaker_uid,
                    restored.power_on,
                    restored.current_track,
                    restored.volume,
                )

        self.async_set_updated_data(state)

    async def _async_restore_state(self) -> SoundLightFullState | None:
        """Load persisted S&L state from HA storage."""
        try:
            data = await self._store.async_load()
            if not data or not isinstance(data, dict):
                return None
            kwargs = {}
            for field in _SL_PERSIST_FIELDS:
                if field in data and data[field] is not None:
                    kwargs[field] = data[field]
            if not kwargs:
                return None
            # Convert available_tracks if present
            if "available_tracks" in data and data["available_tracks"]:
                kwargs["available_tracks"] = tuple(data["available_tracks"])
            return SoundLightFullState(**kwargs)
        except Exception:
            _LOGGER.debug("Failed to restore S&L state", exc_info=True)
            return None

    async def _async_save_state(self, state: SoundLightFullState) -> None:
        """Persist current S&L state to HA storage."""
        try:
            data = {}
            for field in _SL_PERSIST_FIELDS:
                val = getattr(state, field, None)
                if val is not None:
                    data[field] = val
            # Also save available_tracks
            if state.available_tracks:
                data["available_tracks"] = list(state.available_tracks)
            if data:
                await self._store.async_save(data)
        except Exception:
            _LOGGER.debug("Failed to save S&L state", exc_info=True)

    @callback
    def _on_sl_event(self, event: SoundLightEvent) -> None:
        """Handle a push event from NanitSoundLight.subscribe()."""
        if event.kind == SoundLightEventKind.CONNECTION_CHANGE:
            transport_connected = self.sound_light.connected
            if transport_connected:
                # Connection is up — cancel any pending unavailability timer
                # and mark connected immediately.
                self._cancel_availability_timer()
                if not self._sl_connected:
                    _LOGGER.info(
                        "S&L %s reconnected",
                        self.sound_light.speaker_uid,
                    )
                self._sl_connected = True
            elif self._sl_connected:
                # Connection just dropped — start the grace period.
                # Don't mark unavailable yet; give the transport time to reconnect.
                _LOGGER.debug(
                    "S&L %s disconnected (grace period %.0fs)",
                    self.sound_light.speaker_uid,
                    _AVAILABILITY_GRACE_SECONDS,
                )
                self._start_availability_timer()
            # If already disconnected and transport still disconnected,
            # do nothing — timer is already running or fired.

        self.async_set_updated_data(event.state)

        # Debounce state saves — at most every 5 seconds to avoid
        # overlapping writes from rapid state/sensor updates.
        if event.kind in (
            SoundLightEventKind.STATE_UPDATE,
            SoundLightEventKind.SENSOR_UPDATE,
        ):
            self._schedule_save(event.state)

    @callback
    def _schedule_save(self, state: SoundLightFullState) -> None:
        """Schedule a debounced state save (at most every 5 seconds)."""
        self._pending_save_state = state
        if self._save_timer is not None:
            # Timer already running — it will pick up the latest state
            return
        self._save_timer = async_call_later(
            self.hass, 5, self._do_save
        )

    @callback
    def _do_save(self, _now: object) -> None:
        """Execute the debounced state save."""
        self._save_timer = None
        if self._pending_save_state is not None:
            state = self._pending_save_state
            self._pending_save_state = None
            self.hass.async_create_task(self._async_save_state(state))

    @callback
    def _on_availability_timeout(self, _now: object) -> None:
        """Grace period expired — mark S&L entities unavailable."""
        self._availability_timer = None
        if not self.sound_light.connected:
            _LOGGER.warning(
                "S&L %s still disconnected after %.0fs grace period",
                self.sound_light.speaker_uid,
                _AVAILABILITY_GRACE_SECONDS,
            )
            self._sl_connected = False
            self.async_update_listeners()

    def _start_availability_timer(self) -> None:
        """Start (or restart) the grace period timer."""
        self._cancel_availability_timer()
        self._availability_timer = async_call_later(
            self.hass, _AVAILABILITY_GRACE_SECONDS, self._on_availability_timeout
        )

    def _cancel_availability_timer(self) -> None:
        """Cancel the grace period timer if running."""
        if self._availability_timer is not None:
            self._availability_timer()
            self._availability_timer = None

    async def async_shutdown(self) -> None:
        """Stop the S&L device and unsubscribe."""
        self._cancel_availability_timer()
        # Cancel debounced save timer and flush final state
        if self._save_timer is not None:
            self._save_timer()
            self._save_timer = None
        self._pending_save_state = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        # Save final state before stopping
        await self._async_save_state(self.sound_light.state)
        await self.sound_light.async_stop()
        await super().async_shutdown()
