"""Token management with proactive refresh for the Nanit API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .exceptions import NanitAuthError

if TYPE_CHECKING:
    from .rest import NanitRestClient


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
        self._expires_at: float = time.monotonic() + expires_in
        self._lock: asyncio.Lock = asyncio.Lock()
        self._callbacks: list[Callable[[str, str], None]] = []

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    def update_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: float = 3600.0,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = time.monotonic() + expires_in

    async def async_get_access_token(
        self, min_ttl: float = 60.0
    ) -> str:
        async with self._lock:
            if time.monotonic() + min_ttl >= self._expires_at:
                await self._async_refresh()
            return self._access_token

    async def async_force_refresh(self) -> None:
        async with self._lock:
            await self._async_refresh()

    async def _async_refresh(self) -> None:
        try:
            tokens = await self._rest.async_refresh_token(
                self._access_token, self._refresh_token
            )
        except NanitAuthError:
            raise
        except Exception as err:
            raise NanitAuthError(f"Token refresh failed: {err}") from err

        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        self._expires_at = time.monotonic() + 3600.0

        for callback in self._callbacks:
            callback(self._access_token, self._refresh_token)

    def on_tokens_refreshed(
        self, callback: Callable[[str, str], None]
    ) -> Callable[[], None]:
        """Register a callback invoked with (access_token, refresh_token) after refresh.

        Returns an unsubscribe function that removes the callback.
        """
        self._callbacks.append(callback)

        def _unsubscribe() -> None:
            self._callbacks.remove(callback)

        return _unsubscribe
