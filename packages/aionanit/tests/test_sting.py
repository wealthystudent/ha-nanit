"""Tests for STING (Breathing Motion Monitoring) status parsing."""

from __future__ import annotations

from typing import Any

from aionanit.models import BreathingState
from aionanit.parsers import _parse_sting_status
from aionanit.proto import Request, RequestType, StingStatus

# A real PUT_STING_STATUS field-23 payload captured from a Nanit Pro camera with
# Breathing Wear active. Ground-truthed: field 10 (breaths_per_minute) read 43,
# matching the app's displayed value at capture time; is_alert was clear.
_REAL_FRAME = (
    b"\x08\x02\x10\x01\x18\x01 \x00*\x06\x08\xf3\x04\x10\xad\x032\x06\x08\xf3\x04"
    b"\x10\xad\x03@\xb0\xdb\xc2\xd2\x06J\x19N301CMN22474TX_1783672150P+X\x00b\n"
    b"A3hGte6wfoh\x86\x04p\xd6\xda\xc2\xd2\x06\x88\x01\x00\x95\x01\xf8\xc1#@\x9d"
    b"\x01\x90\x1e{@"
)


def _sting_request(**kwargs: Any) -> Request:
    return Request(
        id=1,
        type=RequestType.PUT_STING_STATUS,
        sting_status=StingStatus(**kwargs),
    )


def test_parses_breaths_per_minute_and_flags() -> None:
    result = _parse_sting_status(
        _sting_request(
            state=2,
            is_measuring=True,
            is_detected=True,
            is_alert=False,
            breaths_per_minute=43,
        )
    )
    assert isinstance(result, BreathingState)
    assert result.breaths_per_minute == 43
    assert result.is_alert is False
    assert result.is_measuring is True
    assert result.is_detected is True
    assert result.received_at is not None


def test_alert_flag_true() -> None:
    result = _parse_sting_status(_sting_request(is_alert=True, breaths_per_minute=40))
    assert result is not None
    assert result.is_alert is True


def test_zero_bpm_normalised_to_none() -> None:
    result = _parse_sting_status(_sting_request(breaths_per_minute=0, is_measuring=True))
    assert result is not None
    assert result.breaths_per_minute is None


def test_returns_none_without_sting_status() -> None:
    assert _parse_sting_status(Request(id=1, type=RequestType.PUT_STING_STATUS)) is None


def test_returns_none_for_non_request() -> None:
    assert _parse_sting_status(object()) is None


def test_parses_real_captured_frame() -> None:
    request = Request()
    request.ParseFromString(
        Request(
            id=1,
            type=RequestType.PUT_STING_STATUS,
            sting_status=StingStatus.FromString(_REAL_FRAME),
        ).SerializeToString()
    )
    result = _parse_sting_status(request)
    assert result is not None
    assert result.breaths_per_minute == 43
    assert result.is_alert is False
    assert result.is_measuring is True
