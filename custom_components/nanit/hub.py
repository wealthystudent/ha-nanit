"""Hub for the Nanit integration — owns NanitClient lifecycle.

One hub per config entry (one Nanit account). The hub discovers all
babies/cameras on the account and creates a camera + coordinators for each.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

import aiohttp

from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from aionanit import (
    NanitAuthError,
    NanitCamera,
    NanitClient,
    NanitConnectionError,
)
from aionanit.exceptions import NanitCameraUnavailable
from aionanit.models import Baby

from .const import CONF_CAMERA_IPS, CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import NanitCloudCoordinator, NanitPushCoordinator

if TYPE_CHECKING:
    from . import NanitConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class CameraData:
    """Runtime data for a single camera within the account."""

    camera: NanitCamera
    baby: Baby
    push_coordinator: NanitPushCoordinator
    cloud_coordinator: NanitCloudCoordinator | None


class NanitHub:
    """Manages the NanitClient, token persistence, and all camera instances.

    One hub per config entry (one Nanit account). Discovers all
    babies/cameras on the account and creates coordinators for each.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        entry: NanitConfigEntry,
    ) -> None:
        """Initialize the hub with an existing session and config entry."""
        self._hass = hass
        self._entry = entry
        self._client = NanitClient(session)
        self._camera_data: dict[str, CameraData] = {}
        self._babies: list[Baby] = []
        self._unsubscribe_tokens: Callable[[], None] | None = None

    @property
    def client(self) -> NanitClient:
        """Return the underlying NanitClient."""
        return self._client

    @property
    def camera_data(self) -> dict[str, CameraData]:
        """Return per-camera runtime data, keyed by camera_uid."""
        return self._camera_data

    @property
    def babies(self) -> list[Baby]:
        """Return all discovered babies (including ones that failed to connect)."""
        return self._babies

    async def async_setup(self) -> None:
        """Restore tokens, discover babies, create cameras and coordinators.

        Raises:
            NanitAuthError: If tokens are invalid (triggers reauth).
            NanitConnectionError: If ALL cameras fail to connect.
        """
        # Restore tokens from persisted config entry data
        access_token = self._entry.data[CONF_ACCESS_TOKEN]
        refresh_token = self._entry.data[CONF_REFRESH_TOKEN]
        self._client.restore_tokens(access_token, refresh_token)

        # Register callback to persist refreshed tokens
        tm = self._client.token_manager
        if tm is not None:
            self._unsubscribe_tokens = tm.on_tokens_refreshed(
                self._on_tokens_refreshed
            )

        # Fetch babies (also validates tokens)
        babies = await self._client.async_get_babies()

        self._babies = list(babies)

        if not babies:
            _LOGGER.warning("No babies/cameras found on Nanit account")
            return

        # Per-camera IP configuration from options
        camera_ips: dict[str, str] = self._entry.options.get(CONF_CAMERA_IPS, {})

        # Create camera + coordinators for each baby
        failed_cameras: list[str] = []
        for baby in babies:
            try:
                await self._setup_camera(baby, camera_ips.get(baby.camera_uid))
            except NanitAuthError:
                # Auth errors are account-level — propagate immediately
                raise
            except (NanitConnectionError, NanitCameraUnavailable) as err:
                _LOGGER.warning(
                    "Camera %s (%s) failed to connect: %s",
                    baby.name,
                    baby.camera_uid,
                    err,
                )
                failed_cameras.append(baby.name)
                ir.async_create_issue(
                    self._hass,
                    DOMAIN,
                    f"camera_connection_failed_{baby.camera_uid}",
                    is_fixable=False,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="camera_connection_failed",
                    translation_placeholders={
                        "camera_name": baby.name,
                        "error": str(err),
                    },
                )

        if not self._camera_data and failed_cameras:
            raise NanitConnectionError(
                f"All cameras failed to connect: {', '.join(failed_cameras)}"
            )

        if failed_cameras:
            _LOGGER.warning(
                "Some cameras failed to connect: %s. "
                "They will be retried on next reload.",
                ", ".join(failed_cameras),
            )

    async def _setup_camera(
        self,
        baby: Baby,
        camera_ip: str | None,
    ) -> None:
        """Create a camera instance and its coordinators for a single baby."""
        camera = self._client.camera(
            uid=baby.camera_uid,
            baby_uid=baby.uid,
            prefer_local=camera_ip is not None,
            local_ip=camera_ip,
        )

        push_coordinator = NanitPushCoordinator(
            self._hass, self._entry, camera, baby
        )
        await push_coordinator.async_setup()

        cloud_coordinator: NanitCloudCoordinator | None = None
        try:
            cloud_coordinator = NanitCloudCoordinator(
                self._hass, self._entry, self, baby
            )
            await cloud_coordinator.async_config_entry_first_refresh()
        except NanitAuthError:
            raise
        except NanitConnectionError:
            _LOGGER.warning(
                "Cloud coordinator for %s failed to start; cloud sensors disabled",
                baby.name,
            )
            cloud_coordinator = None

        ir.async_delete_issue(
            self._hass, DOMAIN, f"camera_connection_failed_{baby.camera_uid}"
        )

        self._camera_data[baby.camera_uid] = CameraData(
            camera=camera,
            baby=baby,
            push_coordinator=push_coordinator,
            cloud_coordinator=cloud_coordinator,
        )

    @callback
    def _on_tokens_refreshed(self, new_access: str, new_refresh: str) -> None:
        """Persist refreshed tokens to the config entry."""
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_ACCESS_TOKEN: new_access,
                CONF_REFRESH_TOKEN: new_refresh,
            },
        )

    async def async_close(self) -> None:
        """Stop all cameras and clean up."""
        if self._unsubscribe_tokens is not None:
            self._unsubscribe_tokens()
            self._unsubscribe_tokens = None
        await self._client.async_close()
        self._camera_data.clear()
