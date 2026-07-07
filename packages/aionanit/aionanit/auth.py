"""Token management with proactive refresh for the Nanit API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .exceptions import NanitAuthError, NanitConnectionError

if TYPE_CHECKING:
    from .rest import NanitRestClient

_LOGGER = logging.getLogger(__name__)

# Transient (connection-level) refresh failures are retried with these
# delays before the error is surfaced to the caller.
_REFRESH_RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)


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
    def expires_at(self) -> float:
        """Monotonic clock time at which the current access token expires."""
        return self._expires_at

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

    async def async_get_access_token(self, min_ttl: float = 60.0) -> str:
        refreshed = False
        async with self._lock:
            if time.monotonic() + min_ttl >= self._expires_at:
                await self._async_refresh()
                refreshed = True

        if refreshed:
            self._fire_callbacks()

        return self._access_token

    async def async_force_refresh(self) -> None:
        async with self._lock:
            await self._async_refresh()

        self._fire_callbacks()

    def _fire_callbacks(self) -> None:
        for callback in list(self._callbacks):
            try:
                callback(self._access_token, self._refresh_token)
            except Exception:
                _LOGGER.exception("Error in token refresh callback")

    async def _async_refresh(self) -> None:
        """Refresh the token pair, retrying transient connection failures.

        NanitAuthError (invalid/expired credentials) is surfaced immediately;
        connection-level failures are retried briefly, then propagated as
        NanitConnectionError so callers treat them as transient — never as a
        reason to discard credentials.
        """
        last_err: Exception | None = None
        for attempt, delay in enumerate((0.0, *_REFRESH_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                tokens = await self._rest.async_refresh_token(
                    self._access_token, self._refresh_token
                )
            except NanitAuthError:
                raise
            except Exception as err:
                last_err = err
                _LOGGER.debug(
                    "Token refresh attempt %d failed: %s",
                    attempt + 1,
                    err,
                )
                continue

            self._access_token = tokens["access_token"]
            self._refresh_token = tokens["refresh_token"]
            self._expires_at = _expires_at_from_jwt(self._access_token, 3600.0)
            return

        if isinstance(last_err, NanitConnectionError):
            raise last_err
        raise NanitConnectionError(f"Token refresh failed: {last_err}") from last_err

    def on_tokens_refreshed(self, callback: Callable[[str, str], None]) -> Callable[[], None]:
        """Register a callback invoked with (access_token, refresh_token) after refresh.

        Returns an unsubscribe function that removes the callback.
        """
        self._callbacks.append(callback)

        def _unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return _unsubscribe
