"""Tests for NanitRestClient — all endpoints with mocked HTTP."""

from __future__ import annotations

import contextlib
from datetime import date

import pytest
from aiohttp import ClientConnectionError, ClientResponseError, RequestInfo, TooManyRedirects
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from aionanit.exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitMfaRequiredError,
)
from aionanit.models import (
    Baby,
    CareLog,
    CareLogDetail,
    CareLogDetailFetch,
    CareLogEntry,
    CareLogFetch,
    CloudEvent,
    SpeakerCandidate,
)
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


class TestGetBabyDates:
    """The birth date and record creation date, used only to pick where history starts."""

    async def _baby(self, client: NanitRestClient, **fields: object) -> Baby:
        with mock_api() as m:
            m.get(
                BABIES_URL,
                payload={
                    "babies": [{"uid": "baby123", "name": "Test", "camera_uid": "c1", **fields}]
                },
            )
            (baby,) = await client.async_get_babies("token123")
        return baby

    async def test_parses_both_dates(self, client: NanitRestClient) -> None:
        baby = await self._baby(
            client, birthdate="2023-10-01", created_at="2023-11-14T22:13:20.000Z"
        )

        assert baby.birth_date == date(2023, 10, 1)
        assert baby.created_at == 1700000000.0

    async def test_absent_dates(self, client: NanitRestClient) -> None:
        baby = await self._baby(client)

        assert baby == Baby(uid="baby123", name="Test", camera_uid="c1")
        assert baby.birth_date is None
        assert baby.created_at is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2023-10-01", date(2023, 10, 1)),
            ("2023-10-01T00:00:00Z", date(2023, 10, 1)),
            ("2023-10-01T23:30:00-05:00", date(2023, 10, 1)),
            ("2023-10-01T08:00:00", date(2023, 10, 1)),
            ("2023-02-30", None),
            ("1969-12-31", None),
            ("2286-01-01", None),
            ("10/01/2023", None),
            ("soon", None),
            ("", None),
            ("2023-10-01" + " " * 40, None),
            (1696118400, None),
            (True, None),
            (None, None),
        ],
    )
    async def test_birth_date(
        self, client: NanitRestClient, raw: object, expected: date | None
    ) -> None:
        baby = await self._baby(client, birthdate=raw)

        assert baby.birth_date == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2023-11-14T22:13:20Z", 1700000000.0),
            ("2023-11-14T23:13:20+01:00", 1700000000.0),
            (1700000000, 1700000000.0),
            ("2023-11-14T22:13:20", None),  # no offset: not guessed at
            ("2023-11-14", None),
            ("never", None),
            (1700000000000, None),
            (-1, None),
            (False, None),
            (None, None),
        ],
    )
    async def test_created_at(
        self, client: NanitRestClient, raw: object, expected: float | None
    ) -> None:
        baby = await self._baby(client, created_at=raw)

        assert baby.created_at == expected

    async def test_repr_never_shows_the_dates(self, client: NanitRestClient) -> None:
        baby = await self._baby(client, birthdate="2023-10-01", created_at=1700000000)

        assert baby.birth_date is not None
        assert "2023" not in repr(baby)
        assert "1700000000" not in repr(baby)


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
    """Rows are shaped like a live /calendar response, with made-up values."""

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
                status="eligible",
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
                end_timestamp=1700000965.0,
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
            CareLog(
                uid="ok",
                log_type="bottle_feed",
                timestamp=1700000100.0,
                baby_uid="baby123",
                invalid_fields=frozenset({"feed_amount_ml"}),
            )
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
            CareLog(
                uid="b1",
                log_type="bottle_feed",
                timestamp=1700000200.0,
                baby_uid="baby123",
                invalid_fields=frozenset({"feed_amount_ml"}),
            ),
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
            CareLog(
                uid="b1",
                log_type="bottle_feed",
                timestamp=1700000200.0,
                baby_uid="baby123",
                invalid_fields=frozenset({"feed_amount_ml"}),
            ),
            CareLog(
                uid="n1",
                log_type="nursing",
                timestamp=1700000300.0,
                baby_uid="baby123",
                right_duration_s=0.0,
                invalid_fields=frozenset({"left_duration_s"}),
            ),
            CareLog(
                uid="p1",
                log_type="pumping",
                timestamp=1700000400.0,
                baby_uid="baby123",
                right_amount_ml=7.39,
                invalid_fields=frozenset({"duration_s", "left_amount_ml"}),
            ),
        ]

    async def test_get_calendar_measurements_above_a_ceiling_read_as_not_logged(
        self, client: NanitRestClient
    ) -> None:
        """100,000 mL and 7 days are kept; anything above is rejected like a negative value."""
        week = 7 * 24 * 3600
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        {
                            "uid": "b1",
                            "type": "bottle_feed",
                            "time": 1700000200,
                            "feed_amount": 100_000,
                        },
                        {
                            "uid": "b2",
                            "type": "bottle_feed",
                            "time": 1700000250,
                            "feed_amount": 100_000.5,
                        },
                        {
                            "uid": "n1",
                            "type": "nursing",
                            "time": 1700000300,
                            "left_duration": week,
                            "right_duration": week + 1,
                        },
                        {
                            # Each finite, but their sum is not.
                            "uid": "p1",
                            "type": "pumping",
                            "time": 1700000400,
                            "left_amount": 1e308,
                            "right_amount": 1e308,
                            "duration": 1e308,
                        },
                        {
                            "uid": "s1",
                            "type": "sleep",
                            "time": 1700000500,
                            "duration": 1e300,
                            "net_duration": week + 0.5,
                        },
                    ]
                },
            )
            logs = await self._fetch(client)

        assert logs == [
            CareLog(
                uid="b1",
                log_type="bottle_feed",
                timestamp=1700000200.0,
                baby_uid="baby123",
                feed_amount_ml=100_000.0,
            ),
            CareLog(
                uid="b2",
                log_type="bottle_feed",
                timestamp=1700000250.0,
                baby_uid="baby123",
                invalid_fields=frozenset({"feed_amount_ml"}),
            ),
            CareLog(
                uid="n1",
                log_type="nursing",
                timestamp=1700000300.0,
                baby_uid="baby123",
                left_duration_s=float(week),
                invalid_fields=frozenset({"right_duration_s"}),
            ),
            CareLog(
                uid="p1",
                log_type="pumping",
                timestamp=1700000400.0,
                baby_uid="baby123",
                invalid_fields=frozenset({"left_amount_ml", "right_amount_ml", "duration_s"}),
            ),
            CareLog(
                uid="s1",
                log_type="sleep",
                timestamp=1700000500.0,
                baby_uid="baby123",
                invalid_fields=frozenset({"duration_s", "net_duration_s"}),
            ),
        ]

    async def test_get_calendar_parses_sleep_fields(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        {
                            "uid": "s1",
                            "baby_uid": "baby123",
                            "type": "sleep",
                            "time": 1700000100,
                            "created_at": "2023-11-14T22:30:00.000Z",
                            "status": "logged",
                            "duration": 3600,
                            "end_time": 1700003700,
                            "net_duration": 3300,
                        },
                        {
                            "uid": "s2",
                            "type": "semi_auto_sleep",
                            "time": 1700010000,
                            "created_at": "2023-11-15T03:00:00+01:00",
                            "duration": 1800,
                            "end_time": 1700011800,
                            "net_duration": 1700,
                            "start_source": "camera",
                            "end_source": "user",
                        },
                        {
                            "uid": "s3",
                            "type": "auto_sleep",
                            "time": 1700020000,
                            "begin_ts": 1700020000,
                            "end_ts": 1700020600,
                            "duration": 600,
                            "status": "not_eligible",
                        },
                    ]
                },
            )
            logs = await self._fetch(client)

        assert logs == [
            CareLog(
                uid="s1",
                log_type="sleep",
                timestamp=1700000100.0,
                baby_uid="baby123",
                duration_s=3600.0,
                end_timestamp=1700003700.0,
                net_duration_s=3300.0,
                status="logged",
                logged_at=1700001000.0,
            ),
            CareLog(
                uid="s2",
                log_type="semi_auto_sleep",
                timestamp=1700010000.0,
                baby_uid="baby123",
                duration_s=1800.0,
                end_timestamp=1700011800.0,
                net_duration_s=1700.0,
                logged_at=1700013600.0,
                start_source="camera",
                end_source="user",
            ),
            CareLog(
                uid="s3",
                log_type="auto_sleep",
                timestamp=1700020000.0,
                baby_uid="baby123",
                duration_s=600.0,
                end_timestamp=1700020600.0,
                status="not_eligible",
            ),
        ]

    @pytest.mark.parametrize(
        ("ends", "expected"),
        [
            ({"end_ts": 1700000500, "end_time": 1700000900}, 1700000500.0),
            ({"end_time": 1700000900}, 1700000900.0),
            ({"end_ts": None, "end_time": 1700000900}, 1700000900.0),
            # The first end the row carries decides, even when it is unusable.
            ({"end_ts": "later", "end_time": 1700000900}, None),
            ({"end_ts": 1700000000000, "end_time": 1700000900}, None),
            ({"end_ts": 1699999999}, None),  # before the start
            ({"end_ts": 1700000000}, 1700000000.0),  # zero length
            ({"end_time": True}, None),
            ({"end_time": -5}, None),
            ({"end_time": float("nan")}, None),
            ({}, None),
            ({"duration": 600}, None),  # never worked out from a duration
        ],
    )
    async def test_get_calendar_end_timestamp(
        self, client: NanitRestClient, ends: dict[str, object], expected: float | None
    ) -> None:
        row = {"uid": "s1", "type": "sleep", "time": 1700000000, **ends}
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": [row]})
            (log,) = await self._fetch(client)

        assert log.end_timestamp == expected

    @pytest.mark.parametrize(
        ("created_at", "expected"),
        [
            ("2023-11-14T22:30:00Z", 1700001000.0),
            ("2023-11-14T22:30:00.250+00:00", 1700001000.25),
            ("2023-11-14T23:30:00+01:00", 1700001000.0),
            (1700001000, 1700001000.0),
            (1700001000.5, 1700001000.5),
            ("2023-11-14T22:30:00", None),  # no offset: not guessed at
            ("2023-11-14", None),
            ("yesterday", None),
            ("", None),
            ("2023-11-14T22:30:00Z" + " " * 40, None),
            ("1700001000", None),
            ("1969-12-31T23:59:59Z", None),
            ("2300-01-01T00:00:00Z", None),
            (1700001000000, None),
            (True, None),
            (None, None),
            ({"at": 1}, None),
        ],
    )
    async def test_get_calendar_logged_at(
        self, client: NanitRestClient, created_at: object, expected: float | None
    ) -> None:
        row = {"uid": "d1", "type": "diaper_change", "time": 1700000000, "created_at": created_at}
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": [row]})
            (log,) = await self._fetch(client)

        assert log.logged_at == expected

    async def test_get_calendar_logged_at_absent(self, client: NanitRestClient) -> None:
        row = {"uid": "d1", "type": "diaper_change", "time": 1700000000}
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": [row]})
            (log,) = await self._fetch(client)

        assert log.logged_at is None

    async def test_get_calendar_invalid_measurements_differ_from_absent_ones(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        {"uid": "absent", "type": "sleep", "time": 1700000000},
                        {
                            "uid": "null",
                            "type": "sleep",
                            "time": 1700000000,
                            "duration": None,
                            "net_duration": None,
                        },
                        {
                            "uid": "invalid",
                            "type": "sleep",
                            "time": 1700000000,
                            "duration": "an hour",
                            "net_duration": -1,
                            "feed_amount": True,
                            "left_duration": [60],
                            "right_duration": 10**400,
                            "left_amount": {"ml": 5},
                        },
                        {
                            "uid": "valid",
                            "type": "sleep",
                            "time": 1700000000,
                            "duration": 0,
                            "net_duration": 0.5,
                        },
                    ]
                },
            )
            absent, null, invalid, valid = await self._fetch(client)

        assert absent.invalid_fields == frozenset()
        assert null.invalid_fields == frozenset()
        assert invalid.invalid_fields == frozenset(
            {
                "duration_s",
                "net_duration_s",
                "feed_amount_ml",
                "left_duration_s",
                "right_duration_s",
                "left_amount_ml",
            }
        )
        assert invalid.duration_s is None
        assert invalid.net_duration_s is None
        assert valid.invalid_fields == frozenset()
        assert (valid.duration_s, valid.net_duration_s) == (0.0, 0.5)

    @pytest.mark.parametrize(
        "value",
        [
            "Eligible",
            "not eligible",
            "camera<script>",
            "x" * 33,
            "1camera",
            "_camera",
            "camera\n",
            "camëra",
            "",
            7,
            None,
            ["camera"],
        ],
    )
    async def test_get_calendar_tokens_that_are_not_safe_become_absent(
        self, client: NanitRestClient, value: object
    ) -> None:
        row = {
            "uid": "s1",
            "type": "semi_auto_sleep",
            "time": 1700000000,
            "status": value,
            "start_source": value,
            "end_source": value,
            "change_type": value,
        }
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": [row]})
            (log,) = await self._fetch(client)

        assert log == CareLog(
            uid="s1", log_type="semi_auto_sleep", timestamp=1700000000.0, baby_uid="baby123"
        )

    async def test_get_calendar_drops_rows_whose_type_is_not_a_token(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        {"uid": "a", "type": "Diaper_Change", "time": 1700000000},
                        {"uid": "b", "type": "diaper change", "time": 1700000000},
                        {"uid": "c", "type": "<b>nap</b>", "time": 1700000000},
                        {"uid": "d", "type": "x" * 33, "time": 1700000000},
                        {"uid": "e", "type": "", "time": 1700000000},
                        {"uid": "f", "type": "tummy_time2", "time": 1700000000},
                        {"uid": "g", "type": "x" * 32, "time": 1700000000},
                    ]
                },
            )
            logs = await self._fetch(client)

        # Unknown but well-formed kinds are kept.
        assert [log.log_type for log in logs] == ["tummy_time2", "x" * 32]

    async def test_get_calendar_never_keeps_notes_or_names(self, client: NanitRestClient) -> None:
        row = {
            "uid": "d1",
            "baby_uid": "baby123",
            "type": "diaper_change",
            "change_type": "pee",
            "time": 1700000000,
            "created_at": "2023-11-14T22:13:20Z",
            "status": "logged",
            "comment": "MARKER-NOTE-7f3a",
            "user": {"name": "MARKER-NAME-7f3a", "relation": "MARKER-RELATION-7f3a"},
            "source_uid": "MARKER-SOURCE-7f3a",
        }
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": [row]})
            (log,) = await self._fetch(client)

        assert "MARKER" not in repr(log)
        assert all("MARKER" not in str(value) for value in vars(log).values())

    async def test_get_calendar_result_counts_dropped_rows(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(
                CALENDAR_URL,
                payload={
                    "calendar": [
                        "not a row",
                        None,
                        {"type": "diaper_change", "time": 1700000100},
                        {"uid": "x", "type": "Diaper", "time": 1700000100},
                        {"uid": "x", "type": "diaper_change", "time": 1700000100000},
                        {"uid": "ok", "type": "diaper_change", "time": 1700000100},
                        # Kept: an unusable amount is not a malformed row.
                        {"uid": "b1", "type": "bottle_feed", "time": 1700000200, "feed_amount": -1},
                    ]
                },
            )
            result = await client.async_get_calendar_result(
                "token123", "baby123", 1700000000, 1700086400
            )

        assert result == CareLogFetch(
            logs=(
                CareLog(
                    uid="ok", log_type="diaper_change", timestamp=1700000100.0, baby_uid="baby123"
                ),
                CareLog(
                    uid="b1",
                    log_type="bottle_feed",
                    timestamp=1700000200.0,
                    baby_uid="baby123",
                    invalid_fields=frozenset({"feed_amount_ml"}),
                ),
            ),
            start=1700000000,
            end=1700086400,
            dropped=5,
        )

    async def test_get_calendar_result_empty(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": []})
            result = await client.async_get_calendar_result(
                "token123", "baby123", 1700000000, 1700086400
            )

        assert result == CareLogFetch(logs=(), start=1700000000, end=1700086400, dropped=0)

    async def test_get_calendar_result_without_list_is_connection_error(
        self, client: NanitRestClient
    ) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": None})

            with pytest.raises(NanitConnectionError, match="no calendar list"):
                await client.async_get_calendar_result(
                    "token123", "baby123", 1700000000, 1700086400
                )

    async def test_get_calendar_result_unauthorized(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, status=401)

            with pytest.raises(NanitAuthError):
                await client.async_get_calendar_result(
                    "token123", "baby123", 1700000000, 1700086400
                )

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

            with pytest.raises(NanitConnectionError) as err:
                await self._fetch(client)

        # aiohttp's own error quotes the URL, and with it the baby's uid; ours doesn't.
        assert "baby123" in str(err.value.__cause__)
        assert str(err.value) == "Calendar fetch failed with HTTP 403"

    async def test_get_calendar_timeout_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, exception=TimeoutError())

            with pytest.raises(NanitConnectionError) as err:
                await self._fetch(client)

        assert str(err.value) == "Calendar fetch failed: TimeoutError"

    async def test_get_calendar_client_error_never_repeats_the_url(
        self, client: NanitRestClient
    ) -> None:
        """aiohttp's redirect-loop error quotes the request URL, which carries the baby's uid."""
        url = URL(CALENDAR_URL)
        redirect_loop = TooManyRedirects(
            RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url), ()
        )
        assert "baby123" in str(redirect_loop)
        with mock_api() as m:
            m.get(CALENDAR_URL, exception=redirect_loop)

            with pytest.raises(NanitConnectionError) as err:
                await self._fetch(client)

        assert str(err.value) == "Calendar fetch failed: TooManyRedirects"

    async def test_get_calendar_non_json_is_connection_error(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, status=200, body="<html>oops</html>", content_type="text/html")

            with pytest.raises(NanitConnectionError) as err:
                await self._fetch(client)

        # aiohttp's own error quotes the URL, and with it the baby's uid; ours doesn't.
        assert "baby123" in str(err.value.__cause__)
        assert str(err.value) == "Invalid calendar response: ContentTypeError"


class TestGetCalendarDetails:
    """The one path that keeps notes and names. All text here is made up."""

    async def _fetch(self, client: NanitRestClient, rows: list[object]) -> CareLogDetailFetch:
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": rows})
            return await client.async_get_calendar_details(
                "token123", "baby123", 1700000000, 1700086400
            )

    @staticmethod
    def _row(**fields: object) -> dict[str, object]:
        return {"uid": "d1", "type": "diaper_change", "time": 1700000100, **fields}

    async def test_keeps_the_entry_with_its_note_and_logger(self, client: NanitRestClient) -> None:
        result = await self._fetch(
            client,
            [
                self._row(
                    change_type="pee",
                    status="logged",
                    comment="Before the walk",
                    user={"name": "Sam Example", "relation": "guardian"},
                ),
                {"uid": "bad", "time": 1700000100},
            ],
        )

        assert result == CareLogDetailFetch(
            entries=(
                CareLogEntry(
                    log=CareLog(
                        uid="d1",
                        log_type="diaper_change",
                        timestamp=1700000100.0,
                        baby_uid="baby123",
                        change_type="pee",
                        status="logged",
                    ),
                    detail=CareLogDetail(
                        comment="Before the walk",
                        user_name="Sam Example",
                        user_relation="guardian",
                    ),
                ),
            ),
            start=1700000000,
            end=1700086400,
            dropped=1,
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  two\t\tspaces \n and\r\nlines  ", "two spaces and lines"),
            ("bell\x07 and nul\x00 and del\x7f", "bell and nul and del"),
            ("c1\x85next\x9bcsi", "c1 nextcsi"),
            ("right\u202eto left\u202c", "rightto left"),
            ("zero\u200bwidth\u2060joiner\ufeff", "zerowidthjoiner"),
            ("line\u2028sep\u2029para", "line sep para"),
            ("lone \ud800 surrogate", "lone surrogate"),
            ("<b>bold</b> & <script>alert(1)</script>", "<b>bold</b> & <script>alert(1)</script>"),
            ("Café, naïve, 日本語, emoji 🍼", "Café, naïve, 日本語, emoji 🍼"),
            ("", None),
            ("   \t\n ", None),
            ("\u200b\u202e", None),
            (42, None),
            (None, None),
            (["a note"], None),
            ({"text": "a note"}, None),
        ],
    )
    async def test_cleans_the_note(
        self, client: NanitRestClient, raw: object, expected: str | None
    ) -> None:
        result = await self._fetch(client, [self._row(comment=raw)])

        assert result.entries[0].detail.comment == expected

    async def test_caps_oversized_text(self, client: NanitRestClient) -> None:
        result = await self._fetch(
            client,
            [
                self._row(
                    comment="n" * 10_000,
                    user={"name": "a" * 99 + " bcd", "relation": "r" * 51},
                ),
            ],
        )

        detail = result.entries[0].detail
        assert detail.comment == "n" * 500
        # Cut at the cap, then trimmed: no trailing space is left.
        assert detail.user_name == "a" * 99
        assert detail.user_relation == "r" * 50

    @pytest.mark.parametrize(
        "user",
        [
            None,
            "Sam Example",
            ["Sam Example"],
            {},
            {"name": None, "relation": 3},
        ],
    )
    async def test_missing_or_malformed_user(self, client: NanitRestClient, user: object) -> None:
        result = await self._fetch(client, [self._row(user=user)])

        assert result.entries[0].detail == CareLogDetail()

    async def test_row_without_user_or_comment(self, client: NanitRestClient) -> None:
        result = await self._fetch(client, [self._row()])

        assert result.entries[0].detail == CareLogDetail()

    async def test_repr_never_shows_the_text(self, client: NanitRestClient) -> None:
        result = await self._fetch(
            client,
            [
                self._row(
                    comment="MARKER-NOTE-7f3a",
                    user={"name": "MARKER-NAME-7f3a", "relation": "MARKER-RELATION-7f3a"},
                )
            ],
        )

        assert result.entries[0].detail.comment == "MARKER-NOTE-7f3a"
        for shown in (repr(result), str(result), f"{result.entries[0].detail}"):
            assert "MARKER" not in shown

    async def test_skips_malformed_rows_like_the_numbers_only_fetch(
        self, client: NanitRestClient
    ) -> None:
        result = await self._fetch(
            client,
            [
                "not a row",
                {"uid": "x", "type": "Bad Type", "time": 1700000100, "comment": "kept?"},
                self._row(comment="kept"),
            ],
        )

        assert [entry.detail.comment for entry in result.entries] == ["kept"]
        assert result.dropped == 2

    async def test_unauthorized(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, status=401)

            with pytest.raises(NanitAuthError):
                await client.async_get_calendar_details(
                    "token123", "baby123", 1700000000, 1700086400
                )

    async def test_timeout_error_names_only_its_type(self, client: NanitRestClient) -> None:
        with mock_api() as m:
            m.get(CALENDAR_URL, exception=TimeoutError())

            with pytest.raises(NanitConnectionError) as err:
                await client.async_get_calendar_details(
                    "token123", "baby123", 1700000000, 1700086400
                )

        assert str(err.value) == "Calendar fetch failed: TimeoutError"

    async def test_numbers_only_fetches_never_build_the_detail(
        self, client: NanitRestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def forbidden(*args: object, **kwargs: object) -> CareLogDetail:
            raise AssertionError("the numbers-only path built a CareLogDetail")

        monkeypatch.setattr("aionanit.rest.CareLogDetail", forbidden)
        row = self._row(comment="MARKER-NOTE-7f3a", user={"name": "MARKER-NAME-7f3a"})
        with mock_api() as m:
            m.get(CALENDAR_URL, payload={"calendar": [row]})
            m.get(CALENDAR_URL, payload={"calendar": [row]})
            logs = await client.async_get_calendar("token123", "baby123", 1700000000, 1700086400)
            result = await client.async_get_calendar_result(
                "token123", "baby123", 1700000000, 1700086400
            )

        assert [type(log) for log in (*logs, *result.logs)] == [CareLog, CareLog]
        assert "MARKER" not in repr((logs, result))


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
