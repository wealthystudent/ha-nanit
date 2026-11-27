"""Nanit REST API client for cloud endpoints."""

from __future__ import annotations

import asyncio
import email.utils
import json
import math
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import partial
from typing import Any

import aiohttp

from .exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitMfaRequiredError,
    NanitRateLimited,
    NanitRequestRefused,
    NanitResponseTooLarge,
)
from .models import (
    AccountSpeaker,
    BabiesFetch,
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


_RETRY_AFTER_SECONDS_RE = re.compile(r"[0-9]+")
# More significant digits than this is more than the cap, whatever they are.
_MAX_RETRY_AFTER_DIGITS = len(str(NanitRateLimited.MAX_RETRY_AFTER))
# Longer than any HTTP date ("Sun, 06 Nov 1994 08:49:37 GMT" is 29).
_MAX_RETRY_AFTER_LENGTH = 64
# The asctime form of an HTTP date, such as "Sun Nov  6 08:49:37 1994".
_ASCTIME_RE = re.compile(
    r"[A-Za-z]{3} [A-Za-z]{3} (?:[0-9]{2}| [0-9]) [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}"
)


def _retry_after(value: Any) -> int | None:
    """Return the wait a Retry-After header asks for, in whole seconds, or None.

    Either form the header can take: a number of seconds, or an HTTP date,
    counted from now. A number of seconds may have any number of digits.
    An HTTP date is always GMT. A date that ends with the zone "-0000" is
    read as if it ended with "GMT", and a date in the asctime form, which
    has no zone, is read as UTC. Any other date that leaves its zone unknown
    reads as None. The result is clamped to 0 up to
    ``NanitRateLimited.MAX_RETRY_AFTER``; anything else reads as None.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if _RETRY_AFTER_SECONDS_RE.fullmatch(stripped):
        significant = stripped.lstrip("0")
        if len(significant) > _MAX_RETRY_AFTER_DIGITS:
            return NanitRateLimited.MAX_RETRY_AFTER
        return min(int(significant or "0"), NanitRateLimited.MAX_RETRY_AFTER)
    if len(value) > _MAX_RETRY_AFTER_LENGTH:
        return None
    value = stripped
    if value.endswith(" -0000"):
        value = value.removesuffix(" -0000") + " GMT"
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        if not _ASCTIME_RE.fullmatch(value):
            return None
        moment = moment.replace(tzinfo=UTC)
    seconds = math.ceil((moment - datetime.now(UTC)).total_seconds())
    return min(max(seconds, 0), NanitRateLimited.MAX_RETRY_AFTER)


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
    cam = baby_json.get("camera")
    net = cam.get("network") if isinstance(cam, dict) else None
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


def _parse_baby(baby_json: Any) -> Baby | None:
    """Parse one /babies row, or return None when it is not an object with a uid.

    The uid must be a non-empty string: every baby id the integration
    builds (codes, storage keys, request paths) starts from it, so a row
    whose uid is missing, empty, a number or anything else is skipped
    instead of reaching them.

    A baby without a camera (standalone Sound & Light) has no camera_uid
    in its row: a missing or null one is "" so mixed accounts parse
    instead of failing on the camera-less row. A camera_uid that is there
    but not a string skips the row like a bad uid does.
    """
    if not isinstance(baby_json, dict):
        return None
    uid = baby_json.get("uid")
    if not isinstance(uid, str) or not uid:
        return None
    camera_uid = baby_json.get("camera_uid")
    if camera_uid is None:
        camera_uid = ""
    if not isinstance(camera_uid, str):
        return None
    speaker = baby_json.get("speaker")
    speaker_obj = speaker.get("speaker") if isinstance(speaker, dict) else None
    return Baby(
        uid=uid,
        name=_sanitize_name(baby_json.get("name")),
        camera_uid=camera_uid,
        speaker_uid=speaker_obj.get("uid") if isinstance(speaker_obj, dict) else None,
        network=_parse_network(baby_json),
        camera_connected=_parse_camera_connected(baby_json),
        camera_last_seen=_parse_camera_last_seen(baby_json),
        birth_date=_calendar_date(baby_json.get("birthdate")),
        created_at=_timestamp(baby_json.get("created_at")),
    )


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


# The largest /calendar answer read, counted after decompression. Made-up
# rows in Nanit's format take about 290 bytes each, so this is some 55,000
# rows: about ten times what the busiest 90-day request (the longest the
# integration makes) could hold, and 60 a day for 90 days is 5,400. A larger
# answer is refused unread rather than held in memory and parsed.
MAX_CALENDAR_BYTES = 16 * 1024 * 1024

# Above this an answer is decoded and parsed in the loop's default executor.
# Measured with the event loop's debug mode off, an answer of 1,000 rows
# (about 280 KB) held the loop for 7 ms (12 ms keeping notes), 5,000 rows for
# 40 to 80 ms and 20,000 for 170 to 320 ms. At this size it holds the loop
# for about 10 ms at most, and the reader's five-minute polls, a day or a week
# of rows, stay well below it and skip the executor's thread hop.
OFF_LOOP_CALENDAR_BYTES = 256 * 1024

_READ_CHUNK = 64 * 1024

# The content types resp.json() accepts by default.
_JSON_CONTENT_TYPE_RE = re.compile(r"^application/(?:[\w.+-]+?\+)?json")


async def _async_read_at_most(resp: aiohttp.ClientResponse, limit: int) -> bytes | None:
    """Return the whole body, or None as soon as it is known to be over ``limit`` bytes."""
    if resp.content_length is not None and resp.content_length > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.content.iter_chunked(_READ_CHUNK):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _calendar_rows(body: bytes) -> list[Any]:
    """Decode a /calendar body and return its rows, unparsed."""
    failure: str | None = None
    decoded: Any = None
    try:
        decoded = json.loads(body)
    except ValueError as err:  # JSONDecodeError, UnicodeDecodeError
        failure = type(err).__name__
    if failure is not None:
        raise NanitConnectionError(f"Invalid calendar response: {failure}")
    rows = decoded.get("calendar") if isinstance(decoded, dict) else None
    if not isinstance(rows, list):
        raise NanitConnectionError("Invalid calendar response: no calendar list")
    return rows


def _calendar_result(body: bytes, baby_uid: str, start: int, end: int) -> CareLogFetch:
    rows = _calendar_rows(body)
    logs = tuple(log for row in rows if (log := _parse_care_log(row, baby_uid)) is not None)
    return CareLogFetch(logs=logs, start=start, end=end, dropped=len(rows) - len(logs))


def _calendar_details(body: bytes, baby_uid: str, start: int, end: int) -> CareLogDetailFetch:
    rows = _calendar_rows(body)
    entries: list[CareLogEntry] = []
    for row in rows:
        log = _parse_care_log(row, baby_uid)
        if log is not None:
            entries.append(CareLogEntry(log=log, detail=_parse_care_log_detail(row)))
    return CareLogDetailFetch(
        entries=tuple(entries), start=start, end=end, dropped=len(rows) - len(entries)
    )


async def _async_off_loop_if_large[T](size: int, work: Callable[[], T]) -> T:
    """Return ``work()``, run in the loop's default executor when ``size`` is large."""
    if size <= OFF_LOOP_CALENDAR_BYTES:
        return work()
    return await asyncio.get_running_loop().run_in_executor(None, work)


# Headers required by the Nanit API. The API rejects requests without
# nanit-api-version (especially when MFA is enabled) and may reject
# requests with a non-mobile User-Agent.
NANIT_API_HEADERS: dict[str, str] = {
    "nanit-api-version": "1",
    "User-Agent": "Nanit/767 CFNetwork/1498.700.2 Darwin/23.6.0",
}


def _nanit_lite_headers(access_token: str) -> dict[str, str]:
    """Headers the Nanit Sound + Light app sends to its /speakers endpoints."""
    return {
        "accept": "application/json",
        "authorization": f"token {access_token}",
        "accept-charset": "UTF-8",
        "x-nanit-platform": "homeassistant",
        "user-agent": "NanitLite/1.8.0 (com.udisense.nanitlite; build:168; homeassistant)",
        "x-nanit-service": "1.8.0 (168)",
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
        """Fetch the account's babies, skipping malformed rows."""
        return list((await self.async_get_babies_result(access_token)).babies)

    async def async_get_babies_result(
        self,
        access_token: str,
    ) -> BabiesFetch:
        """Fetch what ``async_get_babies`` does, with how many rows were skipped.

        An answer without a list of babies raises NanitConnectionError: it
        is not an account without babies.
        """
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

        rows = body.get("babies") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise NanitConnectionError("Invalid babies response: no babies list")
        # A malformed row is skipped, so it can't take the account's other
        # babies down with it; the count says the list is not the whole account.
        babies = tuple(baby for row in rows if (baby := _parse_baby(row)) is not None)
        return BabiesFetch(babies=babies, dropped=len(rows) - len(babies))

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
        headers = _nanit_lite_headers(access_token)
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
        """Fetch what ``async_get_calendar`` does, with the range and how many rows were skipped.

        An answer larger than ``MAX_CALENDAR_BYTES`` is not read, and raises
        NanitResponseTooLarge. One larger than ``OFF_LOOP_CALENDAR_BYTES``
        is decoded and parsed in the event loop's default executor, so the
        loop is never held for long.
        """
        body = await self._async_get_calendar_body(access_token, baby_uid, start, end)
        return await _async_off_loop_if_large(
            len(body), partial(_calendar_result, body, baby_uid, start, end)
        )

    async def async_get_calendar_details(
        self,
        access_token: str,
        baby_uid: str,
        start: int,
        end: int,
    ) -> CareLogDetailFetch:
        """Fetch like ``async_get_calendar_result``, keeping each entry's note and logger.

        Notes and names are personal data. This is the only method that
        keeps them; use it only where they are meant to be shown. Large
        answers are handled as in ``async_get_calendar_result``.
        """
        body = await self._async_get_calendar_body(access_token, baby_uid, start, end)
        return await _async_off_loop_if_large(
            len(body), partial(_calendar_details, body, baby_uid, start, end)
        )

    async def _async_get_calendar_body(
        self,
        access_token: str,
        baby_uid: str,
        start: int,
        end: int,
    ) -> bytes:
        """Return the body of a /calendar answer, undecoded.

        aiohttp's errors can quote the request URL, and with it the baby's
        uid, which diagnostics and a logged traceback would show
        unredacted. So every error here is fixed text with only an error's
        type or an HTTP status, and is raised outside the handler that
        caught the original: nothing of it is chained as a cause or context.
        The response is released on every way out.
        """
        failure: str | None = None
        try:
            resp = await self._session.get(
                f"{self._base_url}/babies/{baby_uid}/calendar",
                params={"start": start, "end": end},
                headers={**NANIT_API_HEADERS, "Authorization": access_token},
                timeout=_DEFAULT_TIMEOUT,
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            failure = type(err).__name__
        if failure is not None:
            raise NanitConnectionError(f"Calendar fetch failed: {failure}")

        body: bytes | None = None
        try:
            if resp.status == 401:
                raise NanitAuthError("Access token invalid")

            # A refusal and throttling are told apart from other failures,
            # since asking again at once gets the same answer. Only the
            # status, and the wait a 429 asks for, are kept.
            if resp.status == 403:
                raise NanitRequestRefused("Calendar fetch", 403)
            if resp.status == 429:
                raise NanitRateLimited(
                    "Calendar fetch", _retry_after(resp.headers.get("Retry-After"))
                )
            # Like _classify_http_error, without chaining aiohttp's error.
            if not resp.ok:
                raise NanitConnectionError(f"Calendar fetch failed with HTTP {resp.status}")
            # The check resp.json() makes, by the name of the error it raises.
            if not _JSON_CONTENT_TYPE_RE.match(resp.content_type):
                raise NanitConnectionError("Invalid calendar response: ContentTypeError")
            try:
                body = await _async_read_at_most(resp, MAX_CALENDAR_BYTES)
            except (TimeoutError, aiohttp.ClientError) as err:
                failure = type(err).__name__
            if failure is not None:
                raise NanitConnectionError(f"Invalid calendar response: {failure}")
            if body is None:
                # Closed, not released: the rest of the body is never read.
                resp.close()
                raise NanitResponseTooLarge(
                    f"Calendar response larger than {MAX_CALENDAR_BYTES} bytes"
                )
        finally:
            # What raise_for_status() does before it raises: an unread body
            # would keep its connection, and its timeout, until collected. A
            # no-op once the body was read to its end.
            resp.release()
        return body

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

    async def async_get_account_speakers(self, access_token: str) -> list[AccountSpeaker]:
        """List every Sound & Light Machine on the account (GET /speakers).

        Needs no baby, unlike the pairing view. Deleted machines are left
        out. Uses the same NanitLite headers as the udtokens endpoint.
        """
        try:
            resp = await self._session.get(
                f"{self._base_url}/speakers",
                headers=_nanit_lite_headers(access_token),
                timeout=_DEFAULT_TIMEOUT,
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise NanitConnectionError(str(err) or type(err).__name__) from err

        if resp.status == 401:
            raise NanitAuthError("Access token invalid")

        _classify_http_error(resp, "Account speakers fetch")
        try:
            body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise NanitConnectionError(f"Invalid account speakers response: {err}") from err

        rows = body.get("speakers") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            # Not "no speakers": a caller must be able to tell an unexpected
            # shape from a real empty list.
            raise NanitConnectionError("Invalid account speakers response: no speakers list")
        speakers: list[AccountSpeaker] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("deleted_at"):
                continue
            uid = row.get("uid")
            if not isinstance(uid, str) or not uid:
                continue
            baby = row.get("baby")
            # Only an explicit false counts as unattached; anything unexpected
            # is treated as attached, which keeps the machine out of discovery.
            attached = not (isinstance(baby, dict) and baby.get("attached_to_baby") is False)
            speakers.append(
                AccountSpeaker(
                    uid=uid,
                    name=_sanitize_name(row.get("name")),
                    attached_to_baby=attached,
                )
            )
        return speakers
