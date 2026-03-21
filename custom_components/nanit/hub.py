"""Hub for the Nanit integration — owns NanitClient lifecycle."""

from __future__ import annotations

from collections.abc import Callable
import logging

import aiohttp

from aionanit import (
    NanitAuthError,
    NanitCamera,
    NanitClient,
    NanitConnectionError,
)
from aionanit.models import Baby

from .aionanit_sl.sound_light import NanitSoundLight

_LOGGER = logging.getLogger(__name__)


class NanitHub:
    """Manages the NanitClient, token persistence, and camera instances."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
    ) -> None:
        """Initialize the hub with an existing session and tokens."""
        self._client = NanitClient(session)
        self._client.restore_tokens(access_token, refresh_token)
        self._cameras: dict[str, NanitCamera] = {}
        self._sound_lights: dict[str, NanitSoundLight] = {}
        self._unsubscribe_tokens: Callable[[], None] | None = None

    @property
    def client(self) -> NanitClient:
        """Return the underlying NanitClient."""
        return self._client

    def setup_token_callback(
        self,
        callback: Callable[[str, str], None],
    ) -> None:
        """Register a callback for token refreshes."""
        tm = self._client.token_manager
        if tm is not None:
            self._unsubscribe_tokens = tm.on_tokens_refreshed(callback)

    def get_camera(
        self,
        camera_uid: str,
        baby_uid: str,
        *,
        prefer_local: bool = True,
        local_ip: str | None = None,
    ) -> NanitCamera:
        """Get or create a NanitCamera."""
        if camera_uid in self._cameras:
            return self._cameras[camera_uid]

        cam = self._client.camera(
            uid=camera_uid,
            baby_uid=baby_uid,
            prefer_local=prefer_local,
            local_ip=local_ip,
        )
        self._cameras[camera_uid] = cam
        return cam

    def get_sound_light(
        self,
        speaker_uid: str,
        device_ip: str,
    ) -> NanitSoundLight:
        """Get or create a NanitSoundLight."""
        if speaker_uid in self._sound_lights:
            return self._sound_lights[speaker_uid]

        # NanitSoundLight needs token_manager, rest_client, and session from NanitClient
        if self._client.token_manager is None:
            from aionanit import NanitAuthError
            raise NanitAuthError("Not authenticated — call async_login first")

        sl = NanitSoundLight(
            speaker_uid=speaker_uid,
            device_ip=device_ip,
            token_manager=self._client.token_manager,
            rest_client=self._client.rest_client,
            session=self._client._session,
        )
        self._sound_lights[speaker_uid] = sl
        return sl

    async def async_get_babies(self) -> list[Baby]:
        """Fetch babies from the Nanit cloud API."""
        return await self._client.async_get_babies()

    async def async_close(self) -> None:
        """Stop all cameras and clean up."""
        if self._unsubscribe_tokens is not None:
            self._unsubscribe_tokens()
            self._unsubscribe_tokens = None
        await self._client.async_close()
        self._cameras.clear()
        # Also stop S&L instances
        for sl in list(self._sound_lights.values()):
            try:
                await sl.async_stop()
            except Exception:
                _LOGGER.debug("Error stopping S&L during close")
        self._sound_lights.clear()
