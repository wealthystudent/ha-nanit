"""API client for Nanit."""
from __future__ import annotations

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

    async def start_hls(self) -> None:
        """Start HLS stream."""
        # The prompt says: POST /api/hls/start (no body)
        await self._request("POST", "/api/hls/start")

    async def stop_hls(self) -> None:
        """Stop HLS stream."""
        await self._request("POST", "/api/hls/stop")


class NanitAuthClient:
    """Client for Nanit Cloud Auth."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize."""
        self._session = session
        self._base_url = NANIT_API_BASE
        self._headers = {
            "Content-Type": "application/json",
            "Nanit-App-Version": "1.0.0",  # Example header, might be needed
        }

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make a request to the Cloud API."""
        url = f"{self._base_url}{path}"
        merged_headers = self._headers.copy()
        if headers:
            merged_headers.update(headers)

        try:
            async with self._session.request(
                method, url, json=json, headers=merged_headers
            ) as response:
                if response.status == 401:
                    try:
                        data = await response.json()
                        if data.get("mfa_required"):
                            raise NanitMfaRequiredError(data.get("mfa_token", ""))
                    except (ValueError, TypeError):
                        pass
                    raise NanitAuthError("Authentication failed")

                if response.status >= 400:
                    text = await response.text()
                    _LOGGER.error("Cloud API error %s %s: %s", method, url, text)
                    raise NanitAuthError(f"Cloud API error: {response.status}")

                return await response.json() # type: ignore
        except aiohttp.ClientError as err:
            raise NanitConnectionError(f"Connection error: {err}") from err

    async def login(self, email: str, password: str) -> dict[str, Any]:
        """Login."""
        return await self._request(
            "POST", "/login", json={"email": email, "password": password}
        )

    async def verify_mfa(self, mfa_token: str, code: str) -> dict[str, Any]:
        """Verify MFA."""
        return await self._request(
            "POST", "/login/mfa_token", json={"token": mfa_token, "code": code}
        )

    async def refresh_token(self, access_token: str, refresh_token: str) -> dict[str, Any]:
        """Refresh token."""
        return await self._request(
            "POST",
            "/tokens/refresh",
            json={"refresh_token": refresh_token},
            headers={"Authorization": access_token},
        )

    async def get_babies(self, access_token: str) -> list[dict[str, Any]]:
        """Get babies."""
        # The prompt says: GET /babies with Authorization: {access_token} header
        # But wait, usually Authorization header needs 'Bearer ' prefix.
        # The prompt says "Authorization: {access_token} header". I will assume it means the value is the token directly based on prompt,
        # but standard is Bearer. I'll stick to prompt "Authorization: {access_token}".
        # Actually, let's look at `refresh_token` in prompt: "Authorization: {access_token} header".
        # So I will send just the token.
        
        # Wait, usually `get_babies` returns a list.
        # "Get babies: GET /babies ... Returns: list[dict]" implied?
        # The prompt says: "Get babies: GET /babies ...". It doesn't specify return format explicitly but `get_babies(access_token: str) -> list[dict]`
        # in API section.
        
        response = await self._request(
            "GET", "/babies", headers={"Authorization": access_token}
        )
        # response should be the list of babies or a dict containing it?
        # Usually REST APIs return a dict wrapper.
        # But the type hint says list[dict].
        # I'll return response assuming it's the list or extract it if it's a dict.
        if isinstance(response, list):
            return response
        if "babies" in response:
            return response["babies"] # type: ignore
        # Fallback
        return [response] if isinstance(response, dict) else []
