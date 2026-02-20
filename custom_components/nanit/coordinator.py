"""Coordinator for Nanit."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import NanitApiClient, NanitApiError, NanitAuthError, NanitConnectionError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class NanitLocalCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for local Go backend."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, client: NanitApiClient, update_interval: int
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_local",
            update_interval=timedelta(seconds=update_interval),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API."""
        try:
            status = await self.client.get_status()
            sensors = await self.client.get_sensors()
            settings = await self.client.get_settings()
            hls = await self.client.get_hls_status()
            
            return {
                "status": status,
                "sensors": sensors,
                "settings": settings.get("settings", {}),
                "control": settings.get("control", {}),
                "stream": settings.get("stream", {}),
                "hls": hls,
            }

        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except (NanitApiError, NanitConnectionError) as err:
            raise UpdateFailed(err) from err


class NanitCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for cloud events."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, client: NanitApiClient
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_cloud",
            update_interval=timedelta(seconds=60),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API."""
        try:
            return await self.client.get_events()
        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except (NanitApiError, NanitConnectionError) as err:
            raise UpdateFailed(err) from err
