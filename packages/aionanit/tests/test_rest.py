"""Tests for NanitRestClient — all endpoints with mocked HTTP."""

from __future__ import annotations

import pytest
from aiohttp import ClientConnectionError, ClientSession
from aioresponses import aioresponses

from aionanit.exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitMfaRequiredError,
)
from aionanit.models import Baby, CloudEvent
from aionanit.rest import DEFAULT_BASE_URL, NanitRestClient

LOGIN_URL = f"{DEFAULT_BASE_URL}/login"
REFRESH_URL = f"{DEFAULT_BASE_URL}/tokens/refresh"
BABIES_URL = f"{DEFAULT_BASE_URL}/babies"
EVENTS_URL = f"{DEFAULT_BASE_URL}/babies/baby123/messages?limit=20"


@pytest.fixture
async def session():
    async with ClientSession() as s:
        yield s


@pytest.fixture
def client(session: ClientSession) -> NanitRestClient:
    return NanitRestClient(session)


class TestLogin:
    async def test_login_success(self, client: NanitRestClient) -> None:
        with aioresponses() as m:
            m.post(
                LOGIN_URL,
                payload={
                    "access_token": "acc123",
                    "refresh_token": "ref456",
                },
            )
            result = await client.async_login("user@test.com", "pass123")

        assert result == {
            "access_token": "acc123",
            "refresh_token": "ref456",
        }

    async def test_login_invalid_credentials(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.post(LOGIN_URL, status=401)

            with pytest.raises(NanitAuthError, match="Invalid credentials"):
                await client.async_login("user@test.com", "wrong")

    async def test_login_mfa_required(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.post(
                LOGIN_URL,
                payload={"mfa_token": "mfa_abc"},
            )

            with pytest.raises(NanitMfaRequiredError) as exc_info:
                await client.async_login("user@test.com", "pass123")

            assert exc_info.value.mfa_token == "mfa_abc"

    async def test_login_mfa_required_http_482(
        self, client: NanitRestClient
    ) -> None:
        """Nanit returns HTTP 482 for MFA — verify we parse it before raise_for_status."""
        with aioresponses() as m:
            m.post(
                LOGIN_URL,
                status=482,
                payload={"mfa_token": "mfa_482"},
            )

            with pytest.raises(NanitMfaRequiredError) as exc_info:
                await client.async_login("user@test.com", "pass123")

            assert exc_info.value.mfa_token == "mfa_482"

    async def test_login_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.post(LOGIN_URL, exception=ClientConnectionError("DNS failed"))

            with pytest.raises(NanitConnectionError):
                await client.async_login("user@test.com", "pass123")


class TestLoginMfa:
    async def test_login_mfa_success(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.post(
                LOGIN_URL,
                payload={
                    "access_token": "acc_mfa",
                    "refresh_token": "ref_mfa",
                },
            )
            result = await client.async_login_mfa(
                "user@test.com", "pass123", "mfa_token_abc", "123456"
            )

        assert result["access_token"] == "acc_mfa"
        assert result["refresh_token"] == "ref_mfa"


class TestRefreshToken:
    async def test_refresh_success(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.post(
                REFRESH_URL,
                payload={
                    "access_token": "new_acc",
                    "refresh_token": "new_ref",
                },
            )
            result = await client.async_refresh_token("old_acc", "old_ref")

        assert result == {
            "access_token": "new_acc",
            "refresh_token": "new_ref",
        }

    async def test_refresh_token_expired(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.post(REFRESH_URL, status=404)

            with pytest.raises(NanitAuthError, match="Refresh token expired"):
                await client.async_refresh_token("old_acc", "expired_ref")

    async def test_refresh_access_token_invalid(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.post(REFRESH_URL, status=401)

            with pytest.raises(NanitAuthError, match="Access token invalid"):
                await client.async_refresh_token("bad_acc", "ref")


class TestGetBabies:
    async def test_get_babies_success(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [
                        {
                            "uid": "baby123",
                            "name": "Luna",
                            "camera_uid": "cam456",
                        },
                        {
                            "uid": "baby789",
                            "name": "Max",
                            "camera_uid": "cam012",
                        },
                    ]
                },
            )
            babies = await client.async_get_babies("token123")

        assert len(babies) == 2
        assert babies[0] == Baby(uid="baby123", name="Luna", camera_uid="cam456")
        assert babies[1] == Baby(uid="baby789", name="Max", camera_uid="cam012")

    async def test_get_babies_empty(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.get(BABIES_URL, payload={"babies": []})
            babies = await client.async_get_babies("token123")

        assert babies == []

    async def test_get_babies_unauthorized(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.get(BABIES_URL, status=401)

            with pytest.raises(NanitAuthError):
                await client.async_get_babies("bad_token")


class TestGetEvents:
    async def test_get_events_success(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.get(
                EVENTS_URL,
                payload={
                    "messages": [
                        {"type": "MOTION", "time": 1700000000.0},
                        {"type": "SOUND", "time": 1700000060.0},
                    ]
                },
            )
            events = await client.async_get_events("token123", "baby123")

        assert len(events) == 2
        assert events[0] == CloudEvent(
            event_type="MOTION", timestamp=1700000000.0, baby_uid="baby123"
        )
        assert events[1] == CloudEvent(
            event_type="SOUND", timestamp=1700000060.0, baby_uid="baby123"
        )

    async def test_get_events_empty(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.get(EVENTS_URL, payload={"messages": []})
            events = await client.async_get_events("token123", "baby123")

        assert events == []

    async def test_get_events_unauthorized(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.get(EVENTS_URL, status=401)

            with pytest.raises(NanitAuthError):
                await client.async_get_events("bad_token", "baby123")

    async def test_get_events_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with aioresponses() as m:
            m.get(EVENTS_URL, exception=ClientConnectionError("timeout"))

            with pytest.raises(NanitConnectionError):
                await client.async_get_events("token123", "baby123")