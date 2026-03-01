"""Tests for aionanit.client — NanitClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from aionanit.client import NanitClient
from aionanit.exceptions import NanitAuthError, NanitMfaRequiredError
from aionanit.models import Baby


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
        with patch.object(
            client.rest_client,
            "async_login",
            new_callable=AsyncMock,
            side_effect=NanitAuthError("Invalid credentials"),
        ):
            with pytest.raises(NanitAuthError, match="Invalid credentials"):
                await client.async_login("user@example.com", "wrong")

        assert client.token_manager is None

    async def test_login_propagates_mfa_required(self) -> None:
        client, _ = _make_client()
        with patch.object(
            client.rest_client,
            "async_login",
            new_callable=AsyncMock,
            side_effect=NanitMfaRequiredError("mfa_tok_abc"),
        ):
            with pytest.raises(NanitMfaRequiredError):
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
            tokens = await client.async_verify_mfa(
                "user@example.com", "pass", "mfa_tok", "123456"
            )

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
        with patch.object(
            client.rest_client,
            "async_get_babies",
            new_callable=AsyncMock,
            return_value=expected_babies,
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
