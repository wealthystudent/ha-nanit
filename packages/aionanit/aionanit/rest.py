"""Nanit REST API client for cloud endpoints."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from typing import Any

import aiohttp

from .exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitMfaRequiredError,
)
from .models import (
    Baby,
    CareLog,
    CareLogDetail,
    CareLogDetailFetch,
    CareLogEntry,
    CareLogFetch,
    CloudEvent,
    NetworkInfo,
    SpeakerCandidate,
)

DEFAULT_BASE_URL = "https://api.nanit.com"
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _classify_http_error(resp: aiohttp.ClientResponse, what: str) -> None:
    """Raise NanitConnectionError for a non-2xx response.

    Every credential rejection the API expresses is already handled by
    the explicit checks that run before this (401, the refresh 404,
    error bodies). What reaches here is unexpected: server-side
    failures, WAF or CDN interference, and Nanit's subscription-gated
    403s (the probe tooling documents 403 as endpoint-exists-but-
    subscription-gated, not a credential problem). None of those are
    fixable by reauthenticating, so everything classifies as a
    retryable connection error. Mapping any of them to NanitAuthError
    would stop the coordinator and raise a reauth prompt that can
    never succeed.
    """
    try:
        resp.raise_for_status()
    except aiohttp.ClientResponseError as err:
        raise NanitConnectionError(f"{what} failed with HTTP {err.status}") from err


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _extract_error_message(body: dict[str, Any]) -> str | None:
    """Extract error message from an API error response.

    Handles OAuth2-style errors (``error`` + ``error_description``) and
    Nanit's simple error format (``message`` without ``access_token``).
    Returns *None* for success responses.
    """
    # OAuth2-style: {"error": "...", "error_description": "..."}
    if "error" in body:
        error = str(body["error"])
        desc = body.get("error_description")
        return f"{error}: {desc}" if desc else error
    # Nanit simple error: {"message": "..."} without tokens
    if "message" in body and "access_token" not in body:
        return str(body["message"])
    return None


def _sanitize_name(raw: str | None) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    stripped = _HTML_TAG_RE.sub("", raw)
    return "".join(ch for ch in stripped if unicodedata.category(ch)[0] != "C").strip()


def _parse_network(baby_json: dict[str, Any]) -> NetworkInfo | None:
    net = (baby_json.get("camera") or {}).get("network")
    if not isinstance(net, dict):
        return None
    ssid = net.get("ssid")
    freq = net.get("freq")
    level = net.get("level")
    if ssid is None and freq is None and level is None:
        return None
    return NetworkInfo(
        ssid=str(ssid) if ssid is not None else None,
        frequency_mhz=int(freq) if isinstance(freq, int | float) else None,
        signal_dbm=int(level) if isinstance(level, int | float) else None,
    )


def _parse_camera_connected(baby_json: dict[str, Any]) -> bool | None:
    """Return the cloud-reported camera connected state, or None if absent."""
    cam = baby_json.get("camera")
    if not isinstance(cam, dict):
        return None
    val = cam.get("connected")
    return bool(val) if isinstance(val, bool) else None


def _parse_camera_last_seen(baby_json: dict[str, Any]) -> int | None:
    """Return the Unix timestamp of the camera's last cloud contact, or None."""
    cam = baby_json.get("camera")
    if not isinstance(cam, dict):
        return None
    val = cam.get("last_seen")
    return int(val) if isinstance(val, int | float) else None


def _finite_number(value: Any) -> float | None:
    """Return value as a float when it is a finite number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        return None
    return number if math.isfinite(number) else None


# Ceilings no real entry reaches: 100 litres, and a week. A value above one
# is unusable like a negative one, so that adding up an entry's measurements
# (two sides, a day of feeds) can never overflow.
MAX_AMOUNT_ML = 100_000.0
MAX_DURATION_S = 7 * 24 * 3600.0


def _measurement(
    row: dict[str, Any], key: str, name: str, invalid: set[str], ceiling: float
) -> float | None:
    """Return the amount or duration under ``key``, or None when it is missing or unusable.

    A value that is there but unusable (negative, above ``ceiling``, not a
    finite number) adds ``name`` to ``invalid``, so a caller can tell it
    from a missing one. JSON null counts as missing.
    """
    value = row.get(key)
    if value is None:
        return None
    number = _finite_number(value)
    if number is None or number < 0 or number > ceiling:
        invalid.add(name)
        return None
    return number


# Calendar times are Unix seconds. Anything at or past this (the year 2286)
# is not one: a millisecond time, for one, is about a thousand times larger.
_MAX_UNIX_SECONDS = 10_000_000_000

# Longer than any ISO 8601 time with microseconds and an offset.
_MAX_ISO_LENGTH = 40


def _unix_seconds(value: Any) -> float | None:
    """Return value when it is a plausible Unix time in seconds, else None."""
    number = _finite_number(value)
    return number if number is not None and 0 < number < _MAX_UNIX_SECONDS else None


def _timestamp(value: Any) -> float | None:
    """Return a Unix time from Unix seconds or an ISO 8601 string, else None.

    A string without a UTC offset is rejected rather than guessed at.
    """
    if not isinstance(value, str):
        return _unix_seconds(value)
    if len(value) > _MAX_ISO_LENGTH:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return _unix_seconds(moment.timestamp())


# The same span as _MAX_UNIX_SECONDS, for calendar dates.
_EARLIEST_DATE = date(1970, 1, 1)
_LATEST_DATE = date(2285, 12, 31)


def _calendar_date(value: Any) -> date | None:
    """Return the date an ISO 8601 date or date-time string gives, else None.

    A date-time gives the date as written, whatever its offset.
    """
    if not isinstance(value, str) or len(value) > _MAX_ISO_LENGTH:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        try:
            day = datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return day if _EARLIEST_DATE <= day <= _LATEST_DATE else None


# Kinds, statuses and sources come from outside. Only a short lowercase
# token is kept, so nothing else can ride along in them.
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _token(value: Any) -> str | None:
    """Return value when it is a short lowercase token, else None."""
    return value if isinstance(value, str) and _TOKEN_RE.fullmatch(value) else None


def _end_timestamp(row: dict[str, Any], start: float) -> float | None:
    """Return the end the row states, from ``end_ts`` before ``end_time``.

    The first of the two the row carries decides: when it is not a Unix
    time in seconds, or is before the start, there is no end. An end is
    never worked out from a duration.
    """
    raw = row.get("end_ts")
    if raw is None:
        raw = row.get("end_time")
    end = _unix_seconds(raw)
    return end if end is not None and end >= start else None


def _parse_care_log(row: Any, baby_uid: str) -> CareLog | None:
    """Parse one /calendar row, or return None when it lacks a uid, type or valid time."""
    if not isinstance(row, dict):
        return None
    uid = row.get("uid")
    log_type = _token(row.get("type"))
    timestamp = _unix_seconds(row.get("time"))
    if not isinstance(uid, str) or not uid or log_type is None or timestamp is None:
        return None
    invalid: set[str] = set()
    return CareLog(
        uid=uid,
        log_type=log_type,
        timestamp=timestamp,
        baby_uid=baby_uid,
        change_type=_token(row.get("change_type")),
        feed_amount_ml=_measurement(row, "feed_amount", "feed_amount_ml", invalid, MAX_AMOUNT_ML),
        left_duration_s=_measurement(
            row, "left_duration", "left_duration_s", invalid, MAX_DURATION_S
        ),
        right_duration_s=_measurement(
            row, "right_duration", "right_duration_s", invalid, MAX_DURATION_S
        ),
        left_amount_ml=_measurement(row, "left_amount", "left_amount_ml", invalid, MAX_AMOUNT_ML),
        right_amount_ml=_measurement(
            row, "right_amount", "right_amount_ml", invalid, MAX_AMOUNT_ML
        ),
        duration_s=_measurement(row, "duration", "duration_s", invalid, MAX_DURATION_S),
        end_timestamp=_end_timestamp(row, timestamp),
        net_duration_s=_measurement(row, "net_duration", "net_duration_s", invalid, MAX_DURATION_S),
        status=_token(row.get("status")),
        logged_at=_timestamp(row.get("created_at")),
        start_source=_token(row.get("start_source")),
        end_source=_token(row.get("end_source")),
        invalid_fields=frozenset(invalid),
    )


# Caps on the personal text of an entry, after cleaning. Notes in the app
# are a line or two; names and relations a few words.
_MAX_COMMENT_LENGTH = 500
_MAX_USER_NAME_LENGTH = 100
_MAX_USER_RELATION_LENGTH = 50

# Control, format (bidirectional overrides, zero-width characters) and
# unpaired surrogate code points. Whitespace among them becomes a space.
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


def _clean_text(value: Any, max_length: int) -> str | None:
    """Return value as bounded plain text, or None when it is not a string or ends up empty.

    Markup is left as it is: this is text, and whoever shows it escapes it.
    """
    if not isinstance(value, str):
        return None
    kept = "".join(
        ch for ch in value if ch.isspace() or unicodedata.category(ch) not in _REMOVED_CATEGORIES
    )
    text = " ".join(kept.split())[:max_length].rstrip()
    return text or None


def _parse_care_log_detail(row: dict[str, Any]) -> CareLogDetail:
    """Return the cleaned note and logger of a /calendar row that parsed as an entry."""
    user = row.get("user")
    if not isinstance(user, dict):
        user = {}
    return CareLogDetail(
        comment=_clean_text(row.get("comment"), _MAX_COMMENT_LENGTH),
        user_name=_clean_text(user.get("name"), _MAX_USER_NAME_LENGTH),
        user_relation=_clean_text(user.get("relation"), _MAX_USER_RELATION_LENGTH),
    )


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
        except (TimeoutError, aiohttp.ClientError) as err:
            raise NanitConnectionError(str(err) or type(err).__name__) from err

        if resp.status == 401:
            raise NanitAuthError("Invalid credentials")

        # Nanit returns HTTP 482 when MFA is required. Parse the body
        # before raise_for_status() since 482 is non-standard and aiohttp
        # would raise ClientResponseError for it.
        try:
            body = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise NanitConnectionError(f"Invalid login response: {err}") from err

        if "mfa_token" in body:
            raise NanitMfaRequiredError(body["mfa_token"])

        error_msg = _extract_error_message(body)
        if error_msg:
            raise NanitAuthError(error_msg)

        _classify_http_error(resp, "Login")

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
        except (TimeoutError, aiohttp.ClientError) as err:
            # TimeoutError: aiohttp's total timeout raises the builtin, not
            # a ClientError subclass. A hung refresh (observed with flaky
            # DNS) is transient and must never read as an auth failure.
            raise NanitConnectionError(str(err) or type(err).__name__) from err

        if resp.status == 404:
            raise NanitAuthError("Refresh token expired")

        if resp.status == 401:
            raise NanitAuthError("Access token invalid during refresh")

        # Server-side failures and throttling are transient — they must not
        # be treated as auth errors, or a Nanit outage (or a rate limit)
        # would force reauth in the caller.
        if resp.status == 429 or resp.status >= 500:
            raise NanitConnectionError(f"Token refresh failed with HTTP {resp.status}")

        try:
            body = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise NanitConnectionError(f"Invalid token refresh response: {err}") from err

        error_msg = _extract_error_message(body)
        if error_msg:
            raise NanitAuthError(error_msg)

        _classify_http_error(resp, "Token refresh")

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
        except (TimeoutError, aiohttp.ClientError) as err:
            raise NanitConnectionError(str(err) or type(err).__name__) from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        if resp.status >= 500:
            raise NanitConnectionError(f"Babies fetch failed with HTTP {resp.status}")

        _classify_http_error(resp, "Babies fetch")
        try:
            body = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise NanitConnectionError(f"Invalid babies response: {err}") from err

        # A baby without a camera (standalone Sound & Light) has no
        # camera_uid in its row — default it to "" so mixed accounts parse
        # instead of raising KeyError on the camera-less row.
        return [
            Baby(
                uid=baby["uid"],
                name=_sanitize_name(baby.get("name")),
                camera_uid=baby.get("camera_uid") or "",
                speaker_uid=((baby.get("speaker") or {}).get("speaker") or {}).get("uid"),
                network=_parse_network(baby),
                camera_connected=_parse_camera_connected(baby),
                camera_last_seen=_parse_camera_last_seen(baby),
                birth_date=_calendar_date(baby.get("birthdate")),
                created_at=_timestamp(baby.get("created_at")),
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
                timeout=_DEFAULT_TIMEOUT,
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise NanitConnectionError(str(err) or type(err).__name__) from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        _classify_http_error(resp, "Device token fetch")
        try:
            body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise NanitConnectionError(f"Invalid udtokens response: {err}") from err
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
        except (TimeoutError, aiohttp.ClientError) as err:
            raise NanitConnectionError(str(err) or type(err).__name__) from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        _classify_http_error(resp, "Events fetch")
        try:
            body = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise NanitConnectionError(f"Invalid events response: {err}") from err

        return [
            CloudEvent(
                event_type=msg["type"],
                timestamp=msg["time"],
                baby_uid=baby_uid,
            )
            for msg in body.get("messages", [])
        ]

    async def async_get_calendar(
        self,
        access_token: str,
        baby_uid: str,
        start: int,
        end: int,
    ) -> list[CareLog]:
        """Fetch a baby's Care Logs and camera sleep sessions between two Unix times.

        Both bounds are required whole seconds: the API answers 422 to a
        missing or fractional one. Rows that lack a uid, type or time, whose
        type is not a short lowercase token, or whose time is not Unix
        seconds, are skipped rather than failing the whole fetch. A negative
        amount or duration, or one above ``MAX_AMOUNT_ML`` or
        ``MAX_DURATION_S``, reads as not logged, and is named in the entry's
        ``invalid_fields``.
        """
        return list((await self.async_get_calendar_result(access_token, baby_uid, start, end)).logs)

    async def async_get_calendar_result(
        self,
        access_token: str,
        baby_uid: str,
        start: int,
        end: int,
    ) -> CareLogFetch:
        """Fetch what ``async_get_calendar`` does, with the range and how many rows were skipped."""
        rows = await self._async_get_calendar_rows(access_token, baby_uid, start, end)
        logs = tuple(log for row in rows if (log := _parse_care_log(row, baby_uid)) is not None)
        return CareLogFetch(logs=logs, start=start, end=end, dropped=len(rows) - len(logs))

    async def async_get_calendar_details(
        self,
        access_token: str,
        baby_uid: str,
        start: int,
        end: int,
    ) -> CareLogDetailFetch:
        """Fetch like ``async_get_calendar_result``, keeping each entry's note and logger.

        Notes and names are personal data. This is the only method that
        keeps them; use it only where they are meant to be shown.
        """
        rows = await self._async_get_calendar_rows(access_token, baby_uid, start, end)
        entries: list[CareLogEntry] = []
        for row in rows:
            log = _parse_care_log(row, baby_uid)
            if log is not None:
                entries.append(CareLogEntry(log=log, detail=_parse_care_log_detail(row)))
        return CareLogDetailFetch(
            entries=tuple(entries), start=start, end=end, dropped=len(rows) - len(entries)
        )

    async def _async_get_calendar_rows(
        self,
        access_token: str,
        baby_uid: str,
        start: int,
        end: int,
    ) -> list[Any]:
        """Return the raw rows of a /calendar answer, unparsed."""
        try:
            resp = await self._session.get(
                f"{self._base_url}/babies/{baby_uid}/calendar",
                params={"start": start, "end": end},
                headers={**NANIT_API_HEADERS, "Authorization": access_token},
                timeout=_DEFAULT_TIMEOUT,
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            # Only the error's type: its text can carry the request URL, and
            # with it the baby's uid, which diagnostics would show unredacted.
            # The messages below are fixed text and an HTTP status.
            raise NanitConnectionError(f"Calendar fetch failed: {type(err).__name__}") from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        _classify_http_error(resp, "Calendar fetch")
        try:
            body = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            # Only the type, for the same reason as above.
            raise NanitConnectionError(f"Invalid calendar response: {type(err).__name__}") from err

        rows = body.get("calendar") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise NanitConnectionError("Invalid calendar response: no calendar list")
        return rows

    async def async_get_speaker_candidates(
        self,
        access_token: str,
        baby_uid: str,
    ) -> list[SpeakerCandidate]:
        """List the account's Sound & Light Machines from a baby's pairing view."""
        try:
            resp = await self._session.get(
                f"{self._base_url}/babies/{baby_uid}/candidates/speakers",
                headers={**NANIT_API_HEADERS, "Authorization": access_token},
                timeout=_DEFAULT_TIMEOUT,
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise NanitConnectionError(str(err) or type(err).__name__) from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        _classify_http_error(resp, "Speaker candidates fetch")
        try:
            body = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise NanitConnectionError(f"Invalid speaker candidates response: {err}") from err

        rows = body.get("speakers") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            # Not "no speakers": a caller must be able to tell an unexpected
            # shape from a real empty list.
            raise NanitConnectionError("Invalid speaker candidates response: no speakers list")
        return [
            SpeakerCandidate(
                uid=row["uid"],
                name=_sanitize_name(row.get("name")),
                status=row.get("status") or "",
            )
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("uid"), str) and row["uid"]
        ]
