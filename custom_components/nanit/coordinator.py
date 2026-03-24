"""Coordinators for the Nanit integration.

NanitPushCoordinator: Push-based coordinator that wraps NanitCamera.subscribe().
    Fires async_set_updated_data on every CameraEvent callback (sensor, settings,
    control, status, connection changes). No polling — all data arrives via
    WebSocket push.

    Entity availability uses a grace period so that brief reconnections (e.g.,
    pre-emptive token refresh) do not surface as "Unavailable" in HA.

NanitCloudCoordinator: Polls the Nanit cloud API for motion/sound events every
    CLOUD_POLL_INTERVAL seconds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from aionanit import NanitAuthError, NanitCamera, NanitConnectionError
from aionanit.models import Baby, CameraEvent, CameraState, CloudEvent

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
