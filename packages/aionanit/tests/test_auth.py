"""Tests for TokenManager — refresh logic, expiry, concurrency, callbacks."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aionanit.auth import TokenManager
from aionanit.exceptions import NanitAuthError


@pytest.fixture
def mock_rest() -> MagicMock:
    rest = MagicMock()
    rest.async_refresh_token = AsyncMock(
        return_value={
            "access_token": "new_access",
            "refresh_token": "new_refresh",
        }
    )
    return rest


@pytest.fixture
def token_manager(mock_rest: MagicMock) -> TokenManager:
    return TokenManager(
        rest=mock_rest,
        access_token="initial_access",
        refresh_token="initial_refresh",
        expires_in=3600.0,
    )


class TestGetAccessToken:
    async def test_returns_current_token_when_not_expired(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        token = await token_manager.async_get_access_token()
        assert token == "initial_access"
        mock_rest.async_refresh_token.assert_not_called()

    async def test_refreshes_when_expired(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        # Set expires_at to now (already expired)
        token_manager._expires_at = time.monotonic() - 1
        token = await token_manager.async_get_access_token()
        assert token == "new_access"
        mock_rest.async_refresh_token.assert_called_once_with(
            "initial_access", "initial_refresh"
        )

    async def test_refreshes_when_within_min_ttl(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        # Set expires_at to 30s from now, but min_ttl is 60s
        token_manager._expires_at = time.monotonic() + 30
        token = await token_manager.async_get_access_token(min_ttl=60.0)
        assert token == "new_access"
        mock_rest.async_refresh_token.assert_called_once()

    async def test_does_not_refresh_when_outside_min_ttl(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        # 120s remaining, 60s min_ttl — should not refresh
        token_manager._expires_at = time.monotonic() + 120
        token = await token_manager.async_get_access_token(min_ttl=60.0)
        assert token == "initial_access"
        mock_rest.async_refresh_token.assert_not_called()


class TestConcurrentRefresh:
    async def test_concurrent_calls_only_refresh_once(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        token_manager._expires_at = time.monotonic() - 1

        # Add a small delay to the refresh to simulate real I/O
        original_return = mock_rest.async_refresh_token.return_value

        async def slow_refresh(*args, **kwargs):
            await asyncio.sleep(0.05)
            return original_return

        mock_rest.async_refresh_token.side_effect = slow_refresh

        results = await asyncio.gather(
            token_manager.async_get_access_token(),
            token_manager.async_get_access_token(),
            token_manager.async_get_access_token(),
        )

        assert all(r == "new_access" for r in results)
        assert mock_rest.async_refresh_token.call_count == 1


class TestForceRefresh:
    async def test_force_refresh_updates_tokens(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        await token_manager.async_force_refresh()
        assert token_manager.access_token == "new_access"
        assert token_manager.refresh_token == "new_refresh"
        mock_rest.async_refresh_token.assert_called_once()


class TestUpdateTokens:
    def test_update_tokens_sets_new_values(
        self, token_manager: TokenManager
    ) -> None:
        token_manager.update_tokens("manual_access", "manual_refresh", 1800.0)
        assert token_manager.access_token == "manual_access"
        assert token_manager.refresh_token == "manual_refresh"


class TestCallbacks:
    async def test_callback_invoked_on_refresh(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        callback = MagicMock()
        token_manager.on_tokens_refreshed(callback)
        token_manager._expires_at = time.monotonic() - 1

        await token_manager.async_get_access_token()

        callback.assert_called_once_with("new_access", "new_refresh")

    async def test_multiple_callbacks_all_invoked(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        cb1 = MagicMock()
        cb2 = MagicMock()
        token_manager.on_tokens_refreshed(cb1)
        token_manager.on_tokens_refreshed(cb2)
        token_manager._expires_at = time.monotonic() - 1

        await token_manager.async_get_access_token()

        cb1.assert_called_once_with("new_access", "new_refresh")
        cb2.assert_called_once_with("new_access", "new_refresh")

    async def test_unsubscribe_removes_callback(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        callback = MagicMock()
        unsubscribe = token_manager.on_tokens_refreshed(callback)
        unsubscribe()
        token_manager._expires_at = time.monotonic() - 1

        await token_manager.async_get_access_token()

        callback.assert_not_called()


class TestRefreshFailure:
    async def test_auth_error_propagated(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        mock_rest.async_refresh_token.side_effect = NanitAuthError(
            "Refresh token expired"
        )
        token_manager._expires_at = time.monotonic() - 1

        with pytest.raises(NanitAuthError, match="Refresh token expired"):
            await token_manager.async_get_access_token()

    async def test_unexpected_error_wrapped_in_auth_error(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        mock_rest.async_refresh_token.side_effect = RuntimeError("network down")
        token_manager._expires_at = time.monotonic() - 1

        with pytest.raises(NanitAuthError, match="Token refresh failed"):
            await token_manager.async_get_access_token()

    async def test_tokens_unchanged_after_failure(
        self, token_manager: TokenManager, mock_rest: MagicMock
    ) -> None:
        mock_rest.async_refresh_token.side_effect = NanitAuthError("expired")
        token_manager._expires_at = time.monotonic() - 1

        with pytest.raises(NanitAuthError):
            await token_manager.async_get_access_token()

        assert token_manager.access_token == "initial_access"
        assert token_manager.refresh_token == "initial_refresh"
