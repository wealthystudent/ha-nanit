"""Tests for NanitRestClient — all endpoints with mocked HTTP."""

from __future__ import annotations

import contextlib

import pytest
from aiohttp import ClientConnectionError, ClientResponseError

from aionanit.exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitMfaRequiredError,
)
from aionanit.models import Baby, CareLog, CloudEvent, SpeakerCandidate
from aionanit.rest import DEFAULT_BASE_URL, NanitRestClient

from .fake_http import FakeSession, mock_api

LOGIN_URL = f"{DEFAULT_BASE_URL}/login"
REFRESH_URL = f"{DEFAULT_BASE_URL}/tokens/refresh"
BABIES_URL = f"{DEFAULT_BASE_URL}/babies"
EVENTS_URL = f"{DEFAULT_BASE_URL}/babies/baby123/messages?limit=20"
CALENDAR_URL = f"{DEFAULT_BASE_URL}/babies/baby123/calendar?start=1700000000&end=1700086400"
CANDIDATES_URL = f"{DEFAULT_BASE_URL}/babies/baby123/candidates/speakers"
DEVICE_TOKEN_URL = f"{DEFAULT_BASE_URL}/speakers/spk001/udtokens"


@pytest.fixture
async def session(request: pytest.FixtureRequest):
    # The Home Assistant test plugin blocks sockets, except connects to
    # 127.0.0.1 once a test asks for pytest-socket's socket_enabled. Run
    # standalone, without that plugin, sockets are open already.
    with contextlib.suppress(pytest.FixtureLookupError):
        request.getfixturevalue("socket_enabled")
    fake = FakeSession()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.close()


@pytest.fixture
def client(session: FakeSession) -> NanitRestClient:
    return NanitRestClient(session)


class TestLogin:
    async def test_login_success(self, client: NanitRestClient) -> None:
        with mock_api() as m:
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

    async def test_login_invalid_credentials(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(LOGIN_URL, status=401)

            with pytest.raises(NanitAuthError, match="Invalid credentials"):
                await client.async_login("user@test.com", "wrong")

    async def test_login_timeout_is_connection_error(self, client: NanitRestClient) -> None:
        """aiohttp's total timeout raises builtin TimeoutError, not ClientError."""
        with mock_api() as m:
            m.post(LOGIN_URL, exception=TimeoutError())

            with pytest.raises(NanitConnectionError):
                await client.async_login("user@test.com", "pass123")

    async def test_login_non_json_response_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        """A non-JSON body (e.g. an HTML error page) is transient, not auth."""
        with mock_api() as m:
            m.post(LOGIN_URL, status=200, body="<html>oops</html>", content_type="text/html")

            with pytest.raises(NanitConnectionError, match="Invalid login response"):
                await client.async_login("user@test.com", "pass123")

    async def test_login_mfa_required(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(
                LOGIN_URL,
                payload={"mfa_token": "mfa_abc"},
            )

            with pytest.raises(NanitMfaRequiredError) as exc_info:
                await client.async_login("user@test.com", "pass123")

            assert exc_info.value.mfa_token == "mfa_abc"

    async def test_login_mfa_required_http_482(self, client: NanitRestClient) -> None:
        """Nanit returns HTTP 482 for MFA — verify we parse it before raise_for_status."""
        with mock_api() as m:
            m.post(
                LOGIN_URL,
                status=482,
                payload={"mfa_token": "mfa_482"},
            )

            with pytest.raises(NanitMfaRequiredError) as exc_info:
                await client.async_login("user@test.com", "pass123")

            assert exc_info.value.mfa_token == "mfa_482"

    async def test_login_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(LOGIN_URL, exception=ClientConnectionError("DNS failed"))

            with pytest.raises(NanitConnectionError):
                await client.async_login("user@test.com", "pass123")

    async def test_login_oauth2_error_with_description(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(
                LOGIN_URL,
                status=400,
                payload={
                    "error": "unauthorized_client",
                    "error_description": "Grant type 'implicit' not allowed for the client.",
                },
            )

            with pytest.raises(
                NanitAuthError,
                match=r"unauthorized_client: Grant type 'implicit' not allowed",
            ):
                await client.async_login("user@test.com", "pass123")

    async def test_login_oauth2_error_without_description(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(
                LOGIN_URL,
                status=400,
                payload={"error": "invalid_grant"},
            )

            with pytest.raises(NanitAuthError, match="invalid_grant"):
                await client.async_login("user@test.com", "pass123")

    async def test_login_api_message_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(
                LOGIN_URL,
                status=403,
                payload={"message": "account locked"},
            )

            with pytest.raises(NanitAuthError, match="account locked"):
                await client.async_login("user@test.com", "pass123")


class TestLoginMfa:
    async def test_login_mfa_success(self, client: NanitRestClient) -> None:
        with mock_api() as m:
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
    async def test_refresh_success(self, client: NanitRestClient) -> None:
        with mock_api() as m:
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

    async def test_refresh_timeout_is_connection_error(self, client: NanitRestClient) -> None:
        """aiohttp's total timeout raises builtin TimeoutError, not ClientError.

        It must map to NanitConnectionError: a hung refresh is transient and
        must never read as an auth failure (which triggers reauth upstream).
        """
        with mock_api() as m:
            m.post(REFRESH_URL, exception=TimeoutError())

            with pytest.raises(NanitConnectionError):
                await client.async_refresh_token("acc", "ref")

    async def test_refresh_rate_limit_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(REFRESH_URL, status=429, payload={"error": "too many requests"})

            with pytest.raises(NanitConnectionError):
                await client.async_refresh_token("acc", "ref")

    async def test_refresh_token_expired(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(REFRESH_URL, status=404)

            with pytest.raises(NanitAuthError, match="Refresh token expired"):
                await client.async_refresh_token("old_acc", "expired_ref")

    async def test_refresh_access_token_invalid(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(REFRESH_URL, status=401)

            with pytest.raises(NanitAuthError, match="Access token invalid"):
                await client.async_refresh_token("bad_acc", "ref")

    async def test_refresh_oauth2_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(
                REFRESH_URL,
                status=400,
                payload={
                    "error": "unauthorized_client",
                    "error_description": "Grant type 'implicit' not allowed for the client.",
                },
            )

            with pytest.raises(
                NanitAuthError,
                match=r"unauthorized_client: Grant type 'implicit' not allowed",
            ):
                await client.async_refresh_token("acc", "ref")

    async def test_refresh_api_message_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(
                REFRESH_URL,
                status=403,
                payload={"message": "token revoked"},
            )

            with pytest.raises(NanitAuthError, match="token revoked"):
                await client.async_refresh_token("acc", "ref")

    async def test_refresh_server_error_is_connection_error(self, client: NanitRestClient) -> None:
        """A 5xx during refresh is transient — must not trigger reauth."""
        with mock_api() as m:
            m.post(REFRESH_URL, status=502, body="<html>Bad Gateway</html>")

            with pytest.raises(NanitConnectionError, match="HTTP 502"):
                await client.async_refresh_token("acc", "ref")

    async def test_refresh_invalid_body_is_connection_error(self, client: NanitRestClient) -> None:
        """A non-JSON body on a 200 is transient — must not trigger reauth."""
        with mock_api() as m:
            m.post(
                REFRESH_URL,
                status=200,
                body="<html>gateway error</html>",
                content_type="text/html",
            )

            with pytest.raises(NanitConnectionError, match="Invalid token refresh response"):
                await client.async_refresh_token("acc", "ref")


class TestGetBabies:
    async def test_get_babies_success(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [
                        {
                            "uid": "baby123",
                            "name": "Luna",
                            "camera_uid": "cam456",
                            "speaker": {"speaker": {"uid": "spk001"}},
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
        assert babies[0] == Baby(
            uid="baby123", name="Luna", camera_uid="cam456", speaker_uid="spk001"
        )
        assert babies[1] == Baby(uid="baby789", name="Max", camera_uid="cam012", speaker_uid=None)

    async def test_get_babies_speaker_with_null_nested(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [
                        {
                            "uid": "baby1",
                            "name": "Nora",
                            "camera_uid": "cam1",
                            "speaker": {"speaker": None},
                        },
                        {
                            "uid": "baby2",
                            "name": "Eli",
                            "camera_uid": "cam2",
                            "speaker": None,
                        },
                    ]
                },
            )
            babies = await client.async_get_babies("token123")

        assert babies[0].speaker_uid is None
        assert babies[1].speaker_uid is None

    async def test_get_babies_empty(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(BABIES_URL, payload={"babies": []})
            babies = await client.async_get_babies("token123")

        assert babies == []

    async def test_get_babies_null_name(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [
                        {
                            "uid": "baby123",
                            "name": None,
                            "camera_uid": "cam456",
                        },
                    ]
                },
            )
            babies = await client.async_get_babies("token123")

        assert len(babies) == 1
        assert babies[0].name == ""

    async def test_get_babies_unauthorized(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(BABIES_URL, status=401)

            with pytest.raises(NanitAuthError):
                await client.async_get_babies("bad_token")

    async def test_get_babies_camera_connected_true(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [
                        {
                            "uid": "baby1",
                            "name": "Ezra",
                            "camera_uid": "cam1",
                            "camera": {
                                "connected": True,
                                "last_seen": 1783109177,
                            },
                        }
                    ]
                },
            )
            babies = await client.async_get_babies("token123")

        assert babies[0].camera_connected is True
        assert babies[0].camera_last_seen == 1783109177

    async def test_get_babies_camera_connected_false(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [
                        {
                            "uid": "baby1",
                            "name": "Ezra",
                            "camera_uid": "cam1",
                            "camera": {
                                "connected": False,
                                "last_seen": 1783000000,
                            },
                        }
                    ]
                },
            )
            babies = await client.async_get_babies("token123")

        assert babies[0].camera_connected is False
        assert babies[0].camera_last_seen == 1783000000

    async def test_get_babies_camera_connected_absent(self, client: NanitRestClient) -> None:
        """When the camera object is missing, camera_connected and camera_last_seen are None."""
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [
                        {
                            "uid": "baby1",
                            "name": "Ezra",
                            "camera_uid": "cam1",
                        }
                    ]
                },
            )
            babies = await client.async_get_babies("token123")

        assert babies[0].camera_connected is None
        assert babies[0].camera_last_seen is None

    async def test_get_babies_timeout_is_connection_error(self, client: NanitRestClient) -> None:
        """aiohttp's total timeout raises builtin TimeoutError, not ClientError."""
        with mock_api() as m:
            m.get(BABIES_URL, exception=TimeoutError())

            with pytest.raises(NanitConnectionError):
                await client.async_get_babies("token123")

    async def test_get_babies_camera_less_row(self, client: NanitRestClient) -> None:
        """A speaker-only baby has no camera_uid; mixed accounts must parse."""
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [
                        {"uid": "baby1", "name": "Ezra", "camera_uid": "cam1"},
                        {
                            "uid": "baby2",
                            "name": "Mae",
                            "speaker": {"speaker": {"uid": "spk9"}},
                        },
                    ]
                },
            )
            babies = await client.async_get_babies("token123")

        assert babies[0].camera_uid == "cam1"
        assert babies[1].camera_uid == ""
        assert babies[1].speaker_uid == "spk9"

    async def test_get_babies_null_camera_uid_and_missing_name(
        self, client: NanitRestClient
    ) -> None:
        """Explicit null camera_uid and an absent name both parse defensively."""
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={"babies": [{"uid": "baby3", "camera_uid": None}]},
            )
            babies = await client.async_get_babies("token123")

        assert babies[0].camera_uid == ""
        assert babies[0].name == ""


class TestGetEvents:
    async def test_get_events_success(self, client: NanitRestClient) -> None:
        with mock_api() as m:
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

    async def test_get_events_empty(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(EVENTS_URL, payload={"messages": []})
            events = await client.async_get_events("token123", "baby123")

        assert events == []

    async def test_get_events_unauthorized(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(EVENTS_URL, status=401)

            with pytest.raises(NanitAuthError):
                await client.async_get_events("bad_token", "baby123")

    async def test_get_events_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(EVENTS_URL, exception=ClientConnectionError("timeout"))

            with pytest.raises(NanitConnectionError):
                await client.async_get_events("token123", "baby123")

    async def test_get_events_timeout_is_connection_error(self, client: NanitRestClient) -> None:
        """aiohttp's total timeout raises builtin TimeoutError, not ClientError."""
        with mock_api() as m:
            m.get(EVENTS_URL, exception=TimeoutError())

            with pytest.raises(NanitConnectionError):
                await client.async_get_events("token123", "baby123")

    async def test_get_events_non_json_response_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(EVENTS_URL, status=200, body="<html>oops</html>", content_type="text/html")

            with pytest.raises(NanitConnectionError, match="Invalid events response"):
                await client.async_get_events("token123", "baby123")


class TestGetCalendar:
    """Rows are shaped like a live /calendar response (names and notes left out)."""

    async def _fetch(self, client: NanitRestClient) -> list[CareLog]:
        return await client.async_get_calendar("token123", "baby123", 1700000000, 1700086400)

    async def test_get_calendar_parses_each_type(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        {
                            "uid": "d1",
                            "type": "diaper_change",
                            "change_type": "mixed",
                            "time": 1700000100,
                            "status": "eligible",
                            "comment": "",
                            "user": {"name": "Parent", "relation": "dad"},
                        },
                        {
                            "uid": "b1",
                            "type": "bottle_feed",
                            "time": 1700000200,
                            "feed_amount": 118.294,
                        },
                        {
                            "uid": "n1",
                            "type": "nursing",
                            "time": 1700000300,
                            "left_duration": 610,
                            "right_duration": 893,
                        },
                        {
                            "uid": "p1",
                            "type": "pumping",
                            "time": 1700000400,
                            "duration": 900,
                            "left_amount": 7.39,
                            "right_amount": 7.39,
                        },
                        {
                            "uid": "s1",
                            "type": "auto_sleep",
                            "time": 1700000500,
                            "begin_ts": 1700000500,
                            "end_ts": 1700000965,
                            "duration": 465,
                        },
                    ]
                },
            )
            logs = await self._fetch(client)

        assert logs == [
            CareLog(
                uid="d1",
                log_type="diaper_change",
                timestamp=1700000100.0,
                baby_uid="baby123",
                change_type="mixed",
            ),
            CareLog(
                uid="b1",
                log_type="bottle_feed",
                timestamp=1700000200.0,
                baby_uid="baby123",
                feed_amount_ml=118.294,
            ),
            CareLog(
                uid="n1",
                log_type="nursing",
                timestamp=1700000300.0,
                baby_uid="baby123",
                left_duration_s=610.0,
                right_duration_s=893.0,
            ),
            CareLog(
                uid="p1",
                log_type="pumping",
                timestamp=1700000400.0,
                baby_uid="baby123",
                left_amount_ml=7.39,
                right_amount_ml=7.39,
                duration_s=900.0,
            ),
            CareLog(
                uid="s1",
                log_type="auto_sleep",
                timestamp=1700000500.0,
                baby_uid="baby123",
                duration_s=465.0,
            ),
        ]

    async def test_get_calendar_skips_malformed_rows(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        "not a row",
                        {"type": "diaper_change", "time": 1700000100},
                        {"uid": "x", "time": 1700000100},
                        {"uid": "x", "type": "diaper_change", "time": "soon"},
                        {"uid": "x", "type": "diaper_change", "time": True},
                        {
                            "uid": "ok",
                            "type": "bottle_feed",
                            "time": 1700000100,
                            "feed_amount": "lots",
                        },
                    ]
                },
            )
            logs = await self._fetch(client)

        assert logs == [
            CareLog(uid="ok", log_type="bottle_feed", timestamp=1700000100.0, baby_uid="baby123")
        ]

    async def test_get_calendar_skips_rows_without_a_time_in_seconds(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        {"uid": "ms", "type": "diaper_change", "time": 1700000100000},
                        {"uid": "neg", "type": "diaper_change", "time": -1700000100},
                        {"uid": "zero", "type": "diaper_change", "time": 0},
                        {"uid": "", "type": "diaper_change", "time": 1700000100},
                        {"uid": "ok", "type": "diaper_change", "time": 1700000100},
                    ]
                },
            )
            logs = await self._fetch(client)

        assert logs == [
            CareLog(uid="ok", log_type="diaper_change", timestamp=1700000100.0, baby_uid="baby123")
        ]

    async def test_get_calendar_huge_number_keeps_the_rows_around_it(
        self, client: NanitRestClient
    ) -> None:
        """An integer too large for a float is not a number, and never fails the fetch."""
        huge = 10**400
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        {"uid": "d1", "type": "diaper_change", "time": 1700000100},
                        {"uid": "bad", "type": "diaper_change", "time": huge},
                        {
                            "uid": "b1",
                            "type": "bottle_feed",
                            "time": 1700000200,
                            "feed_amount": huge,
                        },
                    ]
                },
            )
            logs = await self._fetch(client)

        assert logs == [
            CareLog(uid="d1", log_type="diaper_change", timestamp=1700000100.0, baby_uid="baby123"),
            CareLog(uid="b1", log_type="bottle_feed", timestamp=1700000200.0, baby_uid="baby123"),
        ]

    async def test_get_calendar_negative_amounts_read_as_not_logged(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        {
                            "uid": "b1",
                            "type": "bottle_feed",
                            "time": 1700000200,
                            "feed_amount": -120,
                        },
                        {
                            "uid": "n1",
                            "type": "nursing",
                            "time": 1700000300,
                            "left_duration": -610,
                            "right_duration": 0,
                        },
                        {
                            "uid": "p1",
                            "type": "pumping",
                            "time": 1700000400,
                            "duration": -900,
                            "left_amount": -7.39,
                            "right_amount": 7.39,
                        },
                    ]
                },
            )
            logs = await self._fetch(client)

        assert logs == [
            CareLog(uid="b1", log_type="bottle_feed", timestamp=1700000200.0, baby_uid="baby123"),
            CareLog(
                uid="n1",
                log_type="nursing",
                timestamp=1700000300.0,
                baby_uid="baby123",
                right_duration_s=0.0,
            ),
            CareLog(
                uid="p1",
                log_type="pumping",
                timestamp=1700000400.0,
                baby_uid="baby123",
                right_amount_ml=7.39,
            ),
        ]

    async def test_get_calendar_empty(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": []})
            assert await self._fetch(client) == []

    async def test_get_calendar_without_list_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"start": "must be a number"})

            with pytest.raises(NanitConnectionError, match="no calendar list"):
                await self._fetch(client)

    async def test_get_calendar_unauthorized(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, status=401)

            with pytest.raises(NanitAuthError):
                await self._fetch(client)

    async def test_get_calendar_forbidden_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, status=403)

            with pytest.raises(NanitConnectionError, match="HTTP 403"):
                await self._fetch(client)

    async def test_get_calendar_timeout_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, exception=TimeoutError())

            with pytest.raises(NanitConnectionError):
                await self._fetch(client)

    async def test_get_calendar_non_json_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, status=200, body="<html>oops</html>", content_type="text/html")

            with pytest.raises(NanitConnectionError, match="Invalid calendar response") as err:
                await self._fetch(client)

        # The message never repeats the URL, which carries the baby's uid.
        assert "baby123" not in str(err.value)


class TestGetDeviceToken:
    async def test_get_device_token_success(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                DEVICE_TOKEN_URL,
                payload={"user_device_token": {"token": "dev_tok_abc"}},
            )
            token = await client.async_get_device_token("acc123", "spk001")

        assert token == "dev_tok_abc"

    async def test_get_device_token_unauthorized(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(DEVICE_TOKEN_URL, status=401)

            with pytest.raises(NanitAuthError, match="Access token invalid"):
                await client.async_get_device_token("bad_token", "spk001")

    async def test_get_device_token_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(DEVICE_TOKEN_URL, exception=ClientConnectionError("timeout"))

            with pytest.raises(NanitConnectionError):
                await client.async_get_device_token("acc123", "spk001")

    async def test_get_device_token_empty_response(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(DEVICE_TOKEN_URL, payload={"user_device_token": {}})

            with pytest.raises(NanitConnectionError, match="No token"):
                await client.async_get_device_token("acc123", "spk001")

    async def test_get_device_token_timeout_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        """aiohttp's total timeout raises builtin TimeoutError, not ClientError."""
        with mock_api() as m:
            m.get(DEVICE_TOKEN_URL, exception=TimeoutError())

            with pytest.raises(NanitConnectionError):
                await client.async_get_device_token("acc123", "spk001")

    async def test_get_device_token_malformed_body_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(DEVICE_TOKEN_URL, status=200, body="not json", content_type="text/html")

            with pytest.raises(NanitConnectionError, match="Invalid udtokens response"):
                await client.async_get_device_token("acc123", "spk001")


class TestHttpErrorClassification:
    """Statuses the explicit checks don't catch must still classify.

    Everything maps to NanitConnectionError: the explicit checks that run
    first already cover every real credential rejection, and Nanit uses
    403 for subscription gating (per the probe tooling), not credentials,
    so auth mapping here would raise a reauth prompt that can never
    succeed. Nothing may escape as a raw aiohttp exception: an
    unclassified 4xx used to hard-fail setup with no retry (or spam
    tracebacks from the events poll).
    """

    async def test_login_forbidden_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(LOGIN_URL, status=403, payload={})
            with pytest.raises(NanitConnectionError, match="HTTP 403"):
                await client.async_login("user@test.com", "pass123")

    async def test_login_unexpected_4xx_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(LOGIN_URL, status=418, payload={})
            with pytest.raises(NanitConnectionError, match="HTTP 418"):
                await client.async_login("user@test.com", "pass123")

    async def test_refresh_forbidden_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.post(REFRESH_URL, status=403, payload={})
            with pytest.raises(NanitConnectionError, match="HTTP 403"):
                await client.async_refresh_token("acc", "ref")

    async def test_refresh_unexpected_4xx_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.post(REFRESH_URL, status=400, payload={})
            with pytest.raises(NanitConnectionError, match="HTTP 400"):
                await client.async_refresh_token("acc", "ref")

    async def test_get_babies_forbidden_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(BABIES_URL, status=403)
            with pytest.raises(NanitConnectionError, match="HTTP 403"):
                await client.async_get_babies("token")

    async def test_get_babies_unexpected_4xx_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(BABIES_URL, status=404)
            with pytest.raises(NanitConnectionError, match="HTTP 404") as excinfo:
                await client.async_get_babies("token")

        assert isinstance(excinfo.value.__cause__, ClientResponseError)

    async def test_get_events_forbidden_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(EVENTS_URL, status=403)
            with pytest.raises(NanitConnectionError, match="HTTP 403"):
                await client.async_get_events("token", "baby123")

    async def test_get_events_unexpected_4xx_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(EVENTS_URL, status=404)
            with pytest.raises(NanitConnectionError, match="HTTP 404"):
                await client.async_get_events("token", "baby123")

    async def test_get_device_token_forbidden_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(DEVICE_TOKEN_URL, status=403)
            with pytest.raises(NanitConnectionError, match="HTTP 403"):
                await client.async_get_device_token("token", "spk001")

    async def test_get_device_token_unexpected_4xx_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(DEVICE_TOKEN_URL, status=404)
            with pytest.raises(NanitConnectionError, match="HTTP 404"):
                await client.async_get_device_token("token", "spk001")


class TestGetSpeakerCandidates:
    async def test_parses_every_status(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                CANDIDATES_URL,
                payload={
                    "speakers": [
                        {"uid": "spk_a", "name": "Bedroom", "status": "currently-coupled"},
                        {"uid": "spk_b", "name": "Living Room", "status": "available"},
                        {"uid": "spk_c", "name": None, "status": "already-coupled"},
                    ]
                },
            )
            candidates = await client.async_get_speaker_candidates("token123", "baby123")

        assert candidates == [
            SpeakerCandidate(uid="spk_a", name="Bedroom", status="currently-coupled"),
            SpeakerCandidate(uid="spk_b", name="Living Room", status="available"),
            SpeakerCandidate(uid="spk_c", name="", status="already-coupled"),
        ]

    async def test_skips_rows_without_a_uid(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                CANDIDATES_URL,
                payload={"speakers": [{"name": "x"}, {"uid": ""}, "junk", {"uid": "spk_b"}]},
            )
            candidates = await client.async_get_speaker_candidates("token123", "baby123")

        assert candidates == [SpeakerCandidate(uid="spk_b", name="", status="")]

    async def test_empty_list_is_authoritative(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CANDIDATES_URL, payload={"speakers": []})
            candidates = await client.async_get_speaker_candidates("token123", "baby123")

        assert candidates == []

    @pytest.mark.parametrize("payload", [{"speakers": None}, {"devices": []}, ["x"]])
    async def test_unexpected_shape_is_connection_error(
        self, client: NanitRestClient, payload: object
    ) -> None:
        with mock_api() as m:
            m.get(CANDIDATES_URL, payload=payload)

            with pytest.raises(NanitConnectionError):
                await client.async_get_speaker_candidates("token123", "baby123")

    async def test_unauthorized(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CANDIDATES_URL, status=401)

            with pytest.raises(NanitAuthError):
                await client.async_get_speaker_candidates("bad_token", "baby123")

    async def test_not_found_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CANDIDATES_URL, status=404)

            with pytest.raises(NanitConnectionError):
                await client.async_get_speaker_candidates("token123", "baby123")
