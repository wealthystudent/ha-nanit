"""Coordinators for the Nanit integration.

NanitPushCoordinator: Push-based coordinator that wraps NanitCamera.subscribe().
    Fires async_set_updated_data on every CameraEvent callback (sensor, settings,
    control, status, connection changes). No polling — all data arrives via
    WebSocket push.

NanitCloudCoordinator: Polls the Nanit cloud API for motion/sound events every
    CLOUD_POLL_INTERVAL seconds.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from aionanit import NanitAuthError, NanitCamera, NanitConnectionError
from aionanit.models import CameraEvent, CameraEventKind, CameraState, CloudEvent

from .const import CLOUD_POLL_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from . import NanitConfigEntry
    from .hub import NanitHub
_LOGGER = logging.getLogger(__name__)


class NanitPushCoordinator(DataUpdateCoordinator[CameraState]):
    """Push-based coordinator that receives state updates from NanitCamera.subscribe().

    No polling is configured — async_set_updated_data() is called by the camera
    callback on every state change. Entity availability is driven by the
    ``connected`` flag which tracks the WebSocket connection state.

    Pattern based on satel_integra (pure push) + Shelly (connected flag for
    availability).
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        camera: NanitCamera,
    ) -> None:
        """Initialize the push coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{camera.uid}",
            # No update_interval — purely push-based
        )
        self.camera = camera
        self.connected: bool = False
        self._unsubscribe: (() -> None) | None = None

    async def async_setup(self) -> None:
        """Start the camera and subscribe to push events.

        Called once from async_setup_entry after the coordinator is created.
        """
        self._unsubscribe = self.camera.subscribe(self._on_camera_event)
        await self.camera.async_start()
        # Seed initial data from the camera's current state
        self.connected = self.camera.connected
        self.async_set_updated_data(self.camera.state)

    @callback
    def _on_camera_event(self, event: CameraEvent) -> None:
        """Handle a push event from NanitCamera.subscribe()."""
        if event.kind == CameraEventKind.CONNECTION_CHANGE:
            self.connected = self.camera.connected
            if not self.connected:
                _LOGGER.debug(
                    "Camera %s disconnected: %s",
                    self.camera.uid,
                    event.state.connection.last_error,
                )
        else:
            # Any data event means the camera is alive
            self.connected = True

        self.async_set_updated_data(event.state)

    async def async_shutdown(self) -> None:
        """Stop the camera and unsubscribe."""
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
        hub: NanitHub,
        baby_uid: str,
    ) -> None:
        """Initialize the cloud coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{baby_uid}_cloud",
            update_interval=timedelta(seconds=CLOUD_POLL_INTERVAL),
        )
        self._hub = hub
        self._baby_uid = baby_uid

    async def _async_update_data(self) -> list[CloudEvent]:
        """Fetch cloud events from the Nanit API."""
        try:
            client = self._hub.client
            token = await client.token_manager.async_get_access_token()
            return await client.rest_client.async_get_events(token, self._baby_uid)
        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except NanitConnectionError as err:
            raise UpdateFailed(f"Cloud event fetch failed: {err}") from err
