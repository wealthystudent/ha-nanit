"""API client for Nanit."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import NANIT_API_BASE

_LOGGER = logging.getLogger(__name__)


class NanitError(Exception):
    """Base class for Nanit errors."""


class NanitApiError(NanitError):
    """API error."""


class NanitConnectionError(NanitError):
    """Connection error."""


class NanitUnavailableError(NanitError):
    """Camera temporarily unavailable (503/504)."""


class NanitAuthError(NanitError):
    """Authentication error."""


class NanitMfaRequiredError(NanitAuthError):
    """MFA required error."""

    def __init__(self, mfa_token: str) -> None:
        """Initialize."""
        super().__init__("MFA required")
        self.mfa_token = mfa_token


class NanitApiClient:
    """API client for local Go backend."""

    def __init__(self, host: str, session: aiohttp.ClientSession) -> None:
        """Initialize."""
        self._host = host.rstrip("/")
        self._session = session

    @property
    def hls_url(self) -> str:
        """Return the HLS stream URL."""
        return f"{self._host}/hls/stream.m3u8"

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make a request to the API."""
        url = f"{self._host}{path}"
        try:
            async with self._session.request(method, url, json=json) as response:
                if response.status in (503, 504):
                    text = await response.text()
                    _LOGGER.debug("Camera unavailable %s %s: %s", method, url, text)
                    raise NanitUnavailableError(
                        f"Camera unavailable: {response.status}"
                    )
                if response.status >= 400:
                    text = await response.text()
                    _LOGGER.error("API error %s %s: %s", method, url, text)
                    raise NanitApiError(f"API error: {response.status} {text}")
                
                # Some endpoints might not return JSON (e.g. streaming stop)
                if response.status == 204:
                    return {}
                    
                try:
                    return await response.json() # type: ignore
                except Exception:
                    # Fallback for empty bodies or non-JSON
                    return {}
        except aiohttp.ClientError as err:
            raise NanitConnectionError(f"Connection error: {err}") from err

    async def get_status(self) -> dict[str, Any]:
        """Get status."""
        return await self._request("GET", "/api/status")

    async def get_sensors(self) -> dict[str, Any]:
        """Get sensors."""
        return await self._request("GET", "/api/sensors")

    async def get_settings(self) -> dict[str, Any]:
        """Get settings."""
        return await self._request("GET", "/api/settings")

    async def get_events(self) -> dict[str, Any]:
        """Get events."""
        return await self._request("GET", "/api/events")

    async def get_hls_status(self) -> dict[str, Any]:
        """Get HLS status."""
        return await self._request("GET", "/api/hls/status")

    async def set_night_light(self, enabled: bool) -> None:
        """Set night light."""
        await self._request("POST", "/api/control/nightlight", json={"enabled": enabled})

    async def set_sleep_mode(self, enabled: bool) -> None:
        """Set sleep mode."""
        await self._request("POST", "/api/control/sleep", json={"enabled": enabled})

    async def set_volume(self, level: int) -> None:
        """Set volume."""
        await self._request("POST", "/api/control/volume", json={"level": level})

    async def set_mic_mute(self, muted: bool) -> None:
        """Set mic mute."""
        await self._request("POST", "/api/control/mic", json={"muted": muted})

    async def set_status_led(self, enabled: bool) -> None:
        """Set status LED."""
        await self._request("POST", "/api/control/statusled", json={"enabled": enabled})

    async def get_snapshot(self) -> bytes | None:
        """Get a JPEG snapshot from the camera."""
        url = f"{self._host}/api/snapshot"
        try:
            async with self._session.get(url) as response:
                if response.status >= 400:
                    _LOGGER.debug("Snapshot error: %s", response.status)
                    return None
                return await response.read()
        except aiohttp.ClientError as err:
            _LOGGER.debug("Snapshot connection error: %s", err)
            return None

    async def start_hls(self) -> None:
        """Start HLS stream."""
        await self._request("POST", "/api/hls/start")

    async def stop_hls(self) -> None:
        """Stop HLS stream."""
        await self._request("POST", "/api/hls/stop")


class NanitAuthClient:
    """Client for Nanit Cloud Auth.

    The Nanit login flow:
    1. POST /login with {email, password} + header nanit-api-version: 1
       - Returns mfa_token in response body (may be 200 or 401)
    2. POST /login with {email, password, mfa_token, mfa_code} + header nanit-api-version: 1
       - Returns {access_token, refresh_token} on success (HTTP 201)
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize."""
        self._session = session
        self._base_url = NANIT_API_BASE
        self._headers = {
            "Content-Type": "application/json",
            "nanit-api-version": "1",
        }

    async def login(self, email: str, password: str) -> dict[str, Any]:
        """Login with email/password. Raises NanitMfaRequiredError if MFA needed."""
        url = f"{self._base_url}/login"
        payload = {"email": email, "password": password}

        try:
            async with self._session.post(
                url, json=payload, headers=self._headers
            ) as response:
                data = await response.json(content_type=None)

                # MFA required: Nanit returns mfa_token in body (status may be 200 or 401)
                mfa_token = data.get("mfa_token") if isinstance(data, dict) else None
                if mfa_token:
                    raise NanitMfaRequiredError(mfa_token)

                # Successful login without MFA (HTTP 201)
                if response.status in (200, 201) and data.get("access_token"):
                    return data

                # Auth failure
                if response.status == 401:
                    raise NanitAuthError("Invalid email or password")

                raise NanitAuthError(f"Login failed: HTTP {response.status}")
        except aiohttp.ClientError as err:
            raise NanitConnectionError(f"Connection error: {err}") from err

    async def verify_mfa(
        self, email: str, password: str, mfa_token: str, code: str
    ) -> dict[str, Any]:
        """Complete MFA by re-posting to /login with all credentials."""
        url = f"{self._base_url}/login"
        payload = {
            "email": email,
            "password": password,
            "mfa_token": mfa_token,
            "mfa_code": code,
        }

        try:
            async with self._session.post(
                url, json=payload, headers=self._headers
            ) as response:
                if response.status in (200, 201):
                    data = await response.json(content_type=None)
                    if data.get("access_token"):
                        return data

                if response.status == 401:
                    raise NanitAuthError("Invalid MFA code")

                text = await response.text()
                raise NanitAuthError(f"MFA verification failed: HTTP {response.status} {text}")
        except aiohttp.ClientError as err:
            raise NanitConnectionError(f"Connection error: {err}") from err

    async def refresh_token(self, access_token: str, refresh_token: str) -> dict[str, Any]:
        """Refresh token."""
        url = f"{self._base_url}/tokens/refresh"
        headers = {
            **self._headers,
            "Authorization": f"Bearer {access_token}",
        }
        payload = {"refresh_token": refresh_token}

        try:
            async with self._session.post(
                url, json=payload, headers=headers
            ) as response:
                if response.status in (200, 201):
                    return await response.json(content_type=None)

                if response.status == 404:
                    raise NanitAuthError("Refresh token expired, re-login required")

                raise NanitAuthError(f"Token refresh failed: HTTP {response.status}")
        except aiohttp.ClientError as err:
            raise NanitConnectionError(f"Connection error: {err}") from err

    async def get_babies(self, access_token: str) -> list[dict[str, Any]]:
        """Get babies list."""
        url = f"{self._base_url}/babies"
        headers = {
            **self._headers,
            "Authorization": f"Bearer {access_token}",
        }

        try:
            async with self._session.get(url, headers=headers) as response:
                if response.status == 401:
                    raise NanitAuthError("Unauthorized")

                if response.status != 200:
                    raise NanitApiError(f"Get babies failed: HTTP {response.status}")

                data = await response.json(content_type=None)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "babies" in data:
                    return data["babies"]
                return []
        except aiohttp.ClientError as err:
            raise NanitConnectionError(f"Connection error: {err}") from err


class NanitAddonClient:
    """Client for communicating with the nanitd add-on HTTP API."""

    def __init__(self, host: str, session: aiohttp.ClientSession) -> None:
        """Initialize."""
        self._host = host.rstrip("/")
        self._session = session

    async def get_auth_status(self) -> dict[str, Any]:
        """GET /api/auth/status — check if nanitd is authenticated and ready."""
        url = f"{self._host}/api/auth/status"
        try:
            async with self._session.get(url) as response:
                if response.status != 200:
                    text = await response.text()
                    raise NanitApiError(
                        f"Auth status check failed: HTTP {response.status} {text}"
                    )
                return await response.json()
        except aiohttp.ClientError as err:
            raise NanitConnectionError(
                f"Cannot reach nanitd at {self._host}: {err}"
            ) from err

    async def provision_token(
        self,
        access_token: str,
        refresh_token: str,
        babies: list[dict[str, Any]],
    ) -> None:
        """POST /api/auth/token — send auth tokens to nanitd."""
        url = f"{self._host}/api/auth/token"
        payload = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "babies": babies,
        }
        try:
            async with self._session.post(url, json=payload) as response:
                if response.status != 200:
                    text = await response.text()
                    raise NanitApiError(
                        f"Token provisioning failed: HTTP {response.status} {text}"
                    )
        except aiohttp.ClientError as err:
            raise NanitConnectionError(
                f"Cannot reach nanitd at {self._host}: {err}"
            ) from err

    async def wait_until_ready(
        self, timeout: float = 30.0, poll_interval: float = 1.0
    ) -> bool:
        """Poll /api/auth/status until nanitd reports ready=true.

        Returns True if ready within timeout, False otherwise.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                status = await self.get_auth_status()
                if status.get("ready"):
                    return True
            except (NanitApiError, NanitConnectionError):
                pass
            await asyncio.sleep(poll_interval)
        return False
