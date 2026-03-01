"""Top-level entrypoint for the aionanit library."""

from __future__ import annotations

import logging

import aiohttp

from .auth import TokenManager
from .camera import NanitCamera
from .exceptions import NanitAuthError
from .models import Baby
from .rest import NanitRestClient

_LOGGER = logging.getLogger(__name__)


class NanitClient:
    """Top-level entrypoint. Creates/manages NanitCamera instances.

    Owns the REST client and TokenManager, delegates camera lifecycle
    to individual NanitCamera objects.

    The caller owns the aiohttp.ClientSession and must close it
    independently.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session: aiohttp.ClientSession = session
        self._rest: NanitRestClient = NanitRestClient(session)
        self._token_manager: TokenManager | None = None
        self._cameras: dict[str, NanitCamera] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def token_manager(self) -> TokenManager | None:
        """Return the current token manager, or None if not authenticated."""
        return self._token_manager

    @property
    def rest_client(self) -> NanitRestClient:
        """Return the underlying REST client."""
        return self._rest

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def async_login(
        self, email: str, password: str
    ) -> dict[str, str]:
        """Login via REST API and create a TokenManager.

        Returns the raw token dict: {"access_token": ..., "refresh_token": ...}.

        Raises:
            NanitMfaRequiredError: If MFA is required.
            NanitAuthError: If credentials are invalid.
            NanitConnectionError: If the API is unreachable.
        """
        tokens = await self._rest.async_login(email, password)
        self._token_manager = TokenManager(
            self._rest,
            tokens["access_token"],
            tokens["refresh_token"],
        )
        return tokens

    async def async_verify_mfa(
        self,
        email: str,
        password: str,
        mfa_token: str,
        mfa_code: str,
    ) -> dict[str, str]:
        """Complete MFA verification and create a TokenManager.

        Returns the raw token dict: {"access_token": ..., "refresh_token": ...}.

        Raises:
            NanitAuthError: If MFA code is invalid.
            NanitConnectionError: If the API is unreachable.
        """
        tokens = await self._rest.async_login_mfa(
            email, password, mfa_token, mfa_code
        )
        self._token_manager = TokenManager(
            self._rest,
            tokens["access_token"],
            tokens["refresh_token"],
        )
        return tokens

    def restore_tokens(
        self, access_token: str, refresh_token: str
    ) -> None:
        """Restore tokens from storage without a login call.

        Creates a TokenManager from previously persisted tokens.
        """
        self._token_manager = TokenManager(
            self._rest,
            access_token,
            refresh_token,
        )

    # ------------------------------------------------------------------
    # Babies
    # ------------------------------------------------------------------

    async def async_get_babies(self) -> list[Baby]:
        """Fetch babies from the Nanit cloud API.

        Raises:
            NanitAuthError: If not authenticated or token is invalid.
            NanitConnectionError: If the API is unreachable.
        """
        if self._token_manager is None:
            raise NanitAuthError("Not authenticated — call async_login first")
        token = await self._token_manager.async_get_access_token()
        return await self._rest.async_get_babies(token)

    # ------------------------------------------------------------------
    # Camera management
    # ------------------------------------------------------------------

    def camera(
        self,
        uid: str,
        baby_uid: str,
        *,
        prefer_local: bool = True,
        local_ip: str | None = None,
    ) -> NanitCamera:
        """Get or create a NanitCamera instance (cached by camera uid).

        Raises:
            NanitAuthError: If not authenticated.
        """
        if self._token_manager is None:
            raise NanitAuthError("Not authenticated — call async_login first")

        if uid in self._cameras:
            return self._cameras[uid]

        cam = NanitCamera(
            uid=uid,
            baby_uid=baby_uid,
            token_manager=self._token_manager,
            rest_client=self._rest,
            session=self._session,
            prefer_local=prefer_local,
            local_ip=local_ip,
        )
        self._cameras[uid] = cam
        return cam

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_close(self) -> None:
        """Stop all cameras and clear the internal cache.

        Does NOT close the aiohttp session — the caller owns it.
        """
        for cam in list(self._cameras.values()):
            try:
                await cam.async_stop()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Error stopping camera %s during close", cam.uid
                )
        self._cameras.clear()
