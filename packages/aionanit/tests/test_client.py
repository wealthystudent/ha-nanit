"""Tests for aionanit.client — NanitClient."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import pytest

from aionanit.client import NanitClient
from aionanit.exceptions import NanitAuthError, NanitConnectionError, NanitMfaRequiredError
from aionanit.models import Baby, CloudEvent


def _make_client() -> tuple[NanitClient, MagicMock]:
    """Create a NanitClient with a mocked session."""
    session = MagicMock(spec=aiohttp.ClientSession)
    client = NanitClient(session)
    return client, session


class TestInit:
    def test_token_manager_is_none_initially(self) -> None:
        client, _ = _make_client()
        assert client.token_manager is None

    def test_rest_client_exists(self) -> None:
        client, _ = _make_client()
        assert client.rest_client is not None


class TestAsyncLogin:
    async def test_login_creates_token_manager(self) -> None:
        client, _ = _make_client()
        with patch.object(
            client.rest_client,
            "async_login",
            new_callable=AsyncMock,
            return_value={
                "access_token": "at123",
                "refresh_token": "rt456",
            },
        ):
            tokens = await client.async_login("user@example.com", "pass")

        assert tokens["access_token"] == "at123"
        assert tokens["refresh_token"] == "rt456"
        assert client.token_manager is not None
        assert client.token_manager.access_token == "at123"
        assert client.token_manager.refresh_token == "rt456"

    async def test_login_propagates_auth_error(self) -> None:
        client, _ = _make_client()
        with (
            patch.object(
                client.rest_client,
                "async_login",
                new_callable=AsyncMock,
                side_effect=NanitAuthError("Invalid credentials"),
            ),
            pytest.raises(NanitAuthError, match="Invalid credentials"),
        ):
            await client.async_login("user@example.com", "wrong")

        assert client.token_manager is None

    async def test_login_propagates_mfa_required(self) -> None:
        client, _ = _make_client()
        with (
            patch.object(
                client.rest_client,
                "async_login",
                new_callable=AsyncMock,
                side_effect=NanitMfaRequiredError("mfa_tok_abc"),
            ),
            pytest.raises(NanitMfaRequiredError),
        ):
            await client.async_login("user@example.com", "pass")

        assert client.token_manager is None


class TestAsyncVerifyMfa:
    async def test_mfa_creates_token_manager(self) -> None:
        client, _ = _make_client()
        with patch.object(
            client.rest_client,
            "async_login_mfa",
            new_callable=AsyncMock,
            return_value={
                "access_token": "mfa_at",
                "refresh_token": "mfa_rt",
            },
        ):
            tokens = await client.async_verify_mfa("user@example.com", "pass", "mfa_tok", "123456")

        assert tokens["access_token"] == "mfa_at"
        assert client.token_manager is not None
        assert client.token_manager.access_token == "mfa_at"


class TestRestoreTokens:
    def test_creates_token_manager(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("stored_at", "stored_rt")

        assert client.token_manager is not None
        assert client.token_manager.access_token == "stored_at"
        assert client.token_manager.refresh_token == "stored_rt"

    def test_overwrites_existing_token_manager(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("old_at", "old_rt")
        client.restore_tokens("new_at", "new_rt")

        assert client.token_manager is not None
        assert client.token_manager.access_token == "new_at"

    def test_restored_tokens_expire_immediately(self) -> None:
        """restore_tokens passes expires_in=0 so first access triggers refresh."""
        import time

        client, _ = _make_client()
        client.restore_tokens("old_at", "old_rt")

        assert client.token_manager is not None
        # expires_in=0 means _expires_at was set to monotonic() at creation,
        # so it should already be expired (or at most equal to now).
        assert client.token_manager._expires_at <= time.monotonic()


class TestAsyncGetBabies:
    async def test_raises_when_not_authenticated(self) -> None:
        client, _ = _make_client()
        with pytest.raises(NanitAuthError, match="Not authenticated"):
            await client.async_get_babies()

    async def test_returns_babies(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("at", "rt")

        expected_babies = [
            Baby(uid="baby1", name="Baby One", camera_uid="cam1"),
        ]
        with (
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                return_value={"access_token": "new_at", "refresh_token": "new_rt"},
            ),
            patch.object(
                client.rest_client,
                "async_get_babies",
                new_callable=AsyncMock,
                return_value=expected_babies,
            ),
        ):
            babies = await client.async_get_babies()

        assert len(babies) == 1
        assert babies[0].uid == "baby1"


class TestCamera:
    def test_raises_when_not_authenticated(self) -> None:
        client, _ = _make_client()
        with pytest.raises(NanitAuthError, match="Not authenticated"):
            client.camera("cam1", "baby1")

    def test_creates_camera(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("at", "rt")

        cam = client.camera("cam1", "baby1")
        assert cam.uid == "cam1"
        assert cam.baby_uid == "baby1"

    def test_caches_camera_by_uid(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("at", "rt")

        cam1 = client.camera("cam1", "baby1")
        cam2 = client.camera("cam1", "baby1")
        assert cam1 is cam2

    def test_different_uids_different_cameras(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("at", "rt")

        cam1 = client.camera("cam1", "baby1")
        cam2 = client.camera("cam2", "baby2")
        assert cam1 is not cam2


class TestAsyncClose:
    async def test_stops_all_cameras(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("at", "rt")

        cam1 = client.camera("cam1", "baby1")
        cam2 = client.camera("cam2", "baby2")

        with (
            patch.object(cam1, "async_stop", new_callable=AsyncMock) as stop1,
            patch.object(cam2, "async_stop", new_callable=AsyncMock) as stop2,
        ):
            await client.async_close()

        stop1.assert_awaited_once()
        stop2.assert_awaited_once()

    async def test_clears_camera_cache(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("at", "rt")
        client.camera("cam1", "baby1")

        cam_mock = client.camera("cam1", "baby1")
        with patch.object(cam_mock, "async_stop", new_callable=AsyncMock):
            await client.async_close()

        # After close, creating camera again should give a new instance
        new_cam = client.camera("cam1", "baby1")
        assert new_cam is not cam_mock

    async def test_handles_stop_errors_gracefully(self) -> None:
        client, _ = _make_client()
        client.restore_tokens("at", "rt")

        cam = client.camera("cam1", "baby1")
        with patch.object(
            cam,
            "async_stop",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            await client.async_close()

    async def test_idempotent(self) -> None:
        client, _ = _make_client()
        # Should not raise when no cameras exist
        await client.async_close()
        await client.async_close()


def _make_authenticated_client() -> NanitClient:
    """Create a NanitClient with a mocked session and a non-expired token."""
    session = MagicMock(spec=aiohttp.ClientSession)
    client = NanitClient(session)
    # Use the internal TokenManager constructor directly so the token is
    # not immediately expired (restore_tokens sets expires_in=0).
    from aionanit.auth import TokenManager

    client._token_manager = TokenManager(client.rest_client, "at", "rt", expires_in=3600.0)
    return client


class TestRetryOn401:
    async def test_get_babies_retries_on_401(self) -> None:
        client = _make_authenticated_client()
        expected = [Baby(uid="b1", name="B", camera_uid="c1")]
        with (
            patch.object(
                client.rest_client,
                "async_get_babies",
                new_callable=AsyncMock,
                side_effect=[NanitAuthError("Access token invalid"), expected],
            ),
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                return_value={"access_token": "fresh_at", "refresh_token": "fresh_rt"},
            ) as mock_refresh,
        ):
            result = await client.async_get_babies()

        assert result == expected
        mock_refresh.assert_awaited_once()

    async def test_get_events_retries_on_401(self) -> None:
        client = _make_authenticated_client()
        expected = [CloudEvent(event_type="MOTION", timestamp=1.0, baby_uid="b1")]
        with (
            patch.object(
                client.rest_client,
                "async_get_events",
                new_callable=AsyncMock,
                side_effect=[NanitAuthError("Access token invalid"), expected],
            ),
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                return_value={"access_token": "fresh_at", "refresh_token": "fresh_rt"},
            ) as mock_refresh,
        ):
            result = await client.async_get_events("b1")

        assert result == expected
        mock_refresh.assert_awaited_once()

    async def test_get_device_token_retries_on_401(self) -> None:
        client = _make_authenticated_client()
        with (
            patch.object(
                client.rest_client,
                "async_get_device_token",
                new_callable=AsyncMock,
                side_effect=[NanitAuthError("Access token invalid"), "device_jwt"],
            ),
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                return_value={"access_token": "fresh_at", "refresh_token": "fresh_rt"},
            ) as mock_refresh,
        ):
            result = await client.async_get_device_token("spk1")

        assert result == "device_jwt"
        mock_refresh.assert_awaited_once()

    async def test_retry_propagates_if_second_attempt_also_401(self) -> None:
        client = _make_authenticated_client()
        with (
            patch.object(
                client.rest_client,
                "async_get_babies",
                new_callable=AsyncMock,
                side_effect=NanitAuthError("Access token invalid"),
            ),
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                return_value={"access_token": "fresh_at", "refresh_token": "fresh_rt"},
            ),
            pytest.raises(NanitAuthError),
        ):
            await client.async_get_babies()

    async def test_no_retry_when_not_authenticated(self) -> None:
        client, _ = _make_client()
        with pytest.raises(NanitAuthError, match="Not authenticated"):
            await client.async_get_events("b1")

    async def test_retry_call_uses_fresh_token(self) -> None:
        """The retry must send the refreshed token, not re-send the failed one."""
        client = _make_authenticated_client()
        expected = [Baby(uid="b1", name="B", camera_uid="c1")]
        with (
            patch.object(
                client.rest_client,
                "async_get_babies",
                new_callable=AsyncMock,
                side_effect=[NanitAuthError("Access token invalid"), expected],
            ) as mock_get_babies,
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                return_value={"access_token": "fresh_at", "refresh_token": "fresh_rt"},
            ),
        ):
            result = await client.async_get_babies()

        assert result == expected
        assert mock_get_babies.await_args_list == [call("at"), call("fresh_at")]

    async def test_refresh_connection_error_surfaces_as_connection_error(self) -> None:
        """A transient refresh failure during the retry is NOT an auth failure.

        Callers map NanitAuthError to a reauth flow, so a DNS blip or
        timeout during the refresh must surface as NanitConnectionError
        (retried on the next poll), never force a spurious reauth.
        """
        client = _make_authenticated_client()
        with (
            patch.object(
                client.rest_client,
                "async_get_babies",
                new_callable=AsyncMock,
                side_effect=NanitAuthError("Access token invalid"),
            ),
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                side_effect=NanitConnectionError("dns down"),
            ),
            pytest.raises(NanitConnectionError, match="dns down"),
        ):
            await client.async_get_babies()

    async def test_refresh_rejection_propagates_auth_error(self) -> None:
        """An explicit refresh rejection is a genuine auth failure and surfaces."""
        client = _make_authenticated_client()
        with (
            patch.object(
                client.rest_client,
                "async_get_babies",
                new_callable=AsyncMock,
                side_effect=NanitAuthError("Access token invalid"),
            ),
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                side_effect=NanitAuthError("Refresh token revoked"),
            ),
            pytest.raises(NanitAuthError, match="Refresh token revoked"),
        ):
            await client.async_get_babies()

    async def test_concurrent_401s_refresh_once(self) -> None:
        """N data calls that 401 on the same token trigger exactly one rotation."""
        client = _make_authenticated_client()
        expected = [Baby(uid="b1", name="B", camera_uid="c1")]

        async def fake_get_babies(token: str) -> list[Baby]:
            # Yield first so every concurrent caller fetches the stale
            # token before the first 401 triggers the refresh.
            await asyncio.sleep(0)
            if token == "at":
                raise NanitAuthError("Access token invalid")
            return expected

        async def slow_refresh(*args: object, **kwargs: object) -> dict[str, str]:
            # Yield while holding the refresh lock so the other callers
            # genuinely contend on it, instead of all arriving after the
            # rotation already completed.
            await asyncio.sleep(0)
            return {"access_token": "fresh_at", "refresh_token": "fresh_rt"}

        with (
            patch.object(
                client.rest_client,
                "async_get_babies",
                new_callable=AsyncMock,
                side_effect=fake_get_babies,
            ),
            patch.object(
                client.rest_client,
                "async_refresh_token",
                new_callable=AsyncMock,
                side_effect=slow_refresh,
            ) as mock_refresh,
        ):
            results = await asyncio.gather(
                client.async_get_babies(),
                client.async_get_babies(),
                client.async_get_babies(),
            )

        assert all(r == expected for r in results)
        mock_refresh.assert_awaited_once()
