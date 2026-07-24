"""Token management with proactive refresh for the Nanit API."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .exceptions import NanitAuthError, NanitConnectionError

if TYPE_CHECKING:
    from .rest import NanitRestClient


def _expires_at_from_jwt(access_token: str, fallback_expires_in: float) -> float:
    """Return monotonic expiry from a JWT exp claim, falling back to expires_in."""
    try:
        _header, payload, _signature = access_token.split(".", 2)
        payload += "=" * (-len(payload) % 4)
        body = json.loads(base64.urlsafe_b64decode(payload.encode()))
        exp = float(body["exp"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return time.monotonic() + fallback_expires_in

    return time.monotonic() + max(exp - time.time(), 0.0)


class TokenManager:
    """Manages access/refresh tokens with automatic proactive renewal.

    Does not own the REST client — caller provides it. Acquires an
    asyncio.Lock around refresh operations to prevent concurrent refreshes.
    """

    def __init__(
        self,
        rest: NanitRestClient,
        access_token: str,
        refresh_token: str,
        expires_in: float = 3600.0,
    ) -> None:
        self._rest: NanitRestClient = rest
        self._access_token: str = access_token
        self._refresh_token: str = refresh_token
        self._expires_at: float = _expires_at_from_jwt(access_token, expires_in)
        self._lock: asyncio.Lock = asyncio.Lock()
        self._callbacks: list[Callable[[str, str], None]] = []

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def expires_in(self) -> float:
        """Seconds until the current access token expires."""
        return max(self._expires_at - time.monotonic(), 0.0)

    async def update_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: float = 3600.0,
    ) -> None:
        async with self._lock:
            self._access_token = access_token
            self._refresh_token = refresh_token
            self._expires_at = _expires_at_from_jwt(access_token, expires_in)

    async def async_get_access_token(self, min_ttl: float = 300.0) -> str:
        """Return a valid access token, refreshing when inside min_ttl.

        The default buffer is five minutes so a transient refresh failure
        leaves headroom for retries (callers naturally retry on their poll
        cadence) instead of racing the token's hard expiry.
        """
        callbacks_to_fire: list[Callable[[str, str], None]] = []
        async with self._lock:
            if time.monotonic() + min_ttl >= self._expires_at:
                await self._async_refresh()
                callbacks_to_fire = list(self._callbacks)

        for callback in callbacks_to_fire:
            callback(self._access_token, self._refresh_token)

        return self._access_token

    async def async_force_refresh(self) -> None:
        async with self._lock:
            await self._async_refresh()

        for callback in self._callbacks:
            callback(self._access_token, self._refresh_token)

    async def _async_refresh(self) -> None:
        try:
            tokens = await self._rest.async_refresh_token(self._access_token, self._refresh_token)
        except (NanitAuthError, NanitConnectionError):
            raise
        except Exception as err:
            # Anything unexpected here is a transport or parsing hiccup, not
            # a credential rejection: NanitAuthError must stay reserved for
            # the explicit rejections the REST layer raises, because callers
            # translate it into a reauth prompt. Wrapping a transient error
            # (e.g. a timeout that escaped aiohttp's ClientError hierarchy)
            # as an auth failure forced users into reauth with a perfectly
            # valid refresh token.
            raise NanitConnectionError(f"Token refresh failed: {err}") from err

        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        self._expires_at = _expires_at_from_jwt(self._access_token, 3600.0)

    def on_tokens_refreshed(self, callback: Callable[[str, str], None]) -> Callable[[], None]:
        """Register a callback invoked with (access_token, refresh_token) after refresh.

        Returns an unsubscribe function that removes the callback.
        """
        self._callbacks.append(callback)

        def _unsubscribe() -> None:
            self._callbacks.remove(callback)

        return _unsubscribe
