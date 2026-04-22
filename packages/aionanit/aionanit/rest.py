"""Nanit REST API client for cloud endpoints."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import aiohttp

from .exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitMfaRequiredError,
)
from .models import Baby, CloudEvent

DEFAULT_BASE_URL = "https://api.nanit.com"
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _sanitize_name(raw: str) -> str:
    stripped = _HTML_TAG_RE.sub("", raw)
    return "".join(ch for ch in stripped if unicodedata.category(ch)[0] != "C").strip()


# Headers required by the Nanit API. The API rejects requests without
# nanit-api-version (especially when MFA is enabled) and may reject
# requests with a non-mobile User-Agent.
NANIT_API_HEADERS: dict[str, str] = {
    "nanit-api-version": "1",
    "User-Agent": "Nanit/767 CFNetwork/1498.700.2 Darwin/23.6.0",
}


class NanitRestClient:
    """Async HTTP client for the Nanit cloud REST API.

    Does not own the aiohttp.ClientSession — caller is responsible
    for creating and closing it.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._session: aiohttp.ClientSession = session
        self._base_url: str = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        """Return the base URL for the Nanit API."""
        return self._base_url

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the underlying aiohttp session."""
        return self._session

    async def async_login(
        self,
        email: str,
        password: str,
    ) -> dict[str, str]:
        data: dict[str, Any] = {"email": email, "password": password}
        return await self._async_auth_request(data)

    async def async_login_mfa(
        self,
        email: str,
        password: str,
        mfa_token: str,
        mfa_code: str,
    ) -> dict[str, str]:
        data: dict[str, Any] = {
            "email": email,
            "password": password,
            "mfa_token": mfa_token,
            "mfa_code": mfa_code,
        }
        return await self._async_auth_request(data)

    async def _async_auth_request(self, data: dict[str, Any]) -> dict[str, str]:
        try:
            resp = await self._session.post(
                f"{self._base_url}/login",
                json=data,
                headers=NANIT_API_HEADERS,
                timeout=_DEFAULT_TIMEOUT,
            )
        except aiohttp.ClientError as err:
            raise NanitConnectionError(str(err)) from err

        if resp.status == 401:
            raise NanitAuthError("Invalid credentials")

        # Nanit returns HTTP 482 when MFA is required. Parse the body
        # before raise_for_status() since 482 is non-standard and aiohttp
        # would raise ClientResponseError for it.
        body = await resp.json()

        if "mfa_token" in body:
            raise NanitMfaRequiredError(body["mfa_token"])

        resp.raise_for_status()

        return {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
        }

    async def async_refresh_token(
        self,
        access_token: str,
        refresh_token: str,
    ) -> dict[str, str]:
        try:
            resp = await self._session.post(
                f"{self._base_url}/tokens/refresh",
                json={"refresh_token": refresh_token},
                headers={**NANIT_API_HEADERS, "Authorization": access_token},
                timeout=_DEFAULT_TIMEOUT,
            )
        except aiohttp.ClientError as err:
            raise NanitConnectionError(str(err)) from err

        if resp.status == 404:
            raise NanitAuthError("Refresh token expired")

        if resp.status == 401:
            raise NanitAuthError("Access token invalid during refresh")

        resp.raise_for_status()
        body = await resp.json()

        return {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
        }

    async def async_get_babies(
        self,
        access_token: str,
    ) -> list[Baby]:
        try:
            resp = await self._session.get(
                f"{self._base_url}/babies",
                headers={**NANIT_API_HEADERS, "Authorization": access_token},
                timeout=_DEFAULT_TIMEOUT,
            )
        except aiohttp.ClientError as err:
            raise NanitConnectionError(str(err)) from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        resp.raise_for_status()
        body = await resp.json()

        return [
            Baby(
                uid=baby["uid"],
                name=_sanitize_name(baby["name"]),
                camera_uid=baby["camera_uid"],
                speaker_uid=((baby.get("speaker") or {}).get("speaker") or {}).get("uid"),
            )
            for baby in body.get("babies", [])
        ]

    async def async_get_device_token(
        self,
        access_token: str,
        speaker_uid: str,
    ) -> str:
        """Get a local device token for a Sound & Light speaker.

        Uses GET with NanitLite-style headers — the endpoint rejects POST
        with HTTP 404.  Returns the RS256 JWT from the
        ``user_device_token.token`` path in the response body.
        """
        # The udtokens endpoint requires NanitLite headers (token prefix,
        # NanitLite User-Agent) rather than the standard NANIT_API_HEADERS.
        headers = {
            "accept": "application/json",
            "authorization": f"token {access_token}",
            "accept-charset": "UTF-8",
            "x-nanit-platform": "homeassistant",
            "user-agent": ("NanitLite/1.8.0 (com.udisense.nanitlite; build:168; homeassistant)"),
            "x-nanit-service": "1.8.0 (168)",
        }
        try:
            resp = await self._session.get(
                f"{self._base_url}/speakers/{speaker_uid}/udtokens",
                headers=headers,
            )
        except aiohttp.ClientError as err:
            raise NanitConnectionError(str(err)) from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        resp.raise_for_status()
        body = await resp.json(content_type=None)
        token: str | None = body.get("user_device_token", {}).get("token")
        if not token:
            raise NanitConnectionError(f"No token in udtokens response for speaker {speaker_uid}")
        return token

    async def async_get_events(
        self,
        access_token: str,
        baby_uid: str,
        limit: int = 20,
    ) -> list[CloudEvent]:
        try:
            resp = await self._session.get(
                f"{self._base_url}/babies/{baby_uid}/messages",
                params={"limit": limit},
                headers={**NANIT_API_HEADERS, "Authorization": access_token},
                timeout=_DEFAULT_TIMEOUT,
            )
        except aiohttp.ClientError as err:
            raise NanitConnectionError(str(err)) from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        resp.raise_for_status()
        body = await resp.json()

        return [
            CloudEvent(
                event_type=msg["type"],
                timestamp=msg["time"],
                baby_uid=baby_uid,
            )
            for msg in body.get("messages", [])
        ]
