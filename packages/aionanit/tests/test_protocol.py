"""Tests for aionanit.ws.protocol — protobuf encode/decode helpers."""

from __future__ import annotations

import pytest

from aionanit.exceptions import NanitProtocolError
from aionanit.proto import (
    GetSensorData,
    GetStatus,
    Message,
    MessageType,
    Request,
    RequestType,
    Response,
    Settings,
)
from aionanit.ws.protocol import (
    build_keepalive,
    build_request,
    decode_message,
    encode_message,
    extract_request,
    extract_response,
)


class TestEncodeDecodeRoundtrip:
    def test_keepalive_roundtrip(self) -> None:
        # betterproto: KEEPALIVE=0 is default enum value,
        # so Message(type=KEEPALIVE) serializes to empty bytes (all defaults).
        msg = Message(type=MessageType.KEEPALIVE)
        data = encode_message(msg)
        assert isinstance(data, bytes)
        # Empty bytes is valid — decodes back to default Message (KEEPALIVE).
        decoded = decode_message(data)
        assert decoded.type == MessageType.KEEPALIVE

    def test_request_roundtrip(self) -> None:
        req = Request(id=42, type=RequestType.GET_STATUS, get_status=GetStatus(all=True))
        msg = Message(type=MessageType.REQUEST, request=req)
        data = encode_message(msg)
        decoded = decode_message(data)
        assert decoded.type == MessageType.REQUEST
        assert decoded.request.id == 42
        assert decoded.request.type == RequestType.GET_STATUS
        assert decoded.request.get_status.all is True

    def test_response_roundtrip(self) -> None:
        resp = Response(request_id=7, request_type=RequestType.GET_SETTINGS, status_code=200)
        msg = Message(type=MessageType.RESPONSE, response=resp)
        data = encode_message(msg)
        decoded = decode_message(data)
        assert decoded.type == MessageType.RESPONSE
        assert decoded.response.request_id == 7
        assert decoded.response.status_code == 200


class TestBuildKeepalive:
    def test_builds_valid_bytes(self) -> None:
        data = build_keepalive()
        assert isinstance(data, bytes)
        msg = decode_message(data)
        assert msg.type == MessageType.KEEPALIVE


class TestBuildRequest:
    def test_get_sensor_data(self) -> None:
        data = build_request(
            request_id=1,
            request_type=RequestType.GET_SENSOR_DATA,
            get_sensor_data=GetSensorData(all=True),
        )
        msg = decode_message(data)
        assert msg.type == MessageType.REQUEST
        assert msg.request.id == 1
        assert msg.request.type == RequestType.GET_SENSOR_DATA
        assert msg.request.get_sensor_data.all is True

    def test_put_settings(self) -> None:
        data = build_request(
            request_id=2,
            request_type=RequestType.PUT_SETTINGS,
            settings=Settings(volume=75, status_light_on=True),
        )
        msg = decode_message(data)
        assert msg.request.type == RequestType.PUT_SETTINGS
        assert msg.request.settings.volume == 75
        assert msg.request.settings.status_light_on is True

    def test_get_status(self) -> None:
        data = build_request(
            request_id=3,
            request_type=RequestType.GET_STATUS,
            get_status=GetStatus(all=True),
        )
        msg = decode_message(data)
        assert msg.request.id == 3
        assert msg.request.type == RequestType.GET_STATUS


class TestExtractResponse:
    def test_returns_response_for_response_message(self) -> None:
        resp = Response(request_id=1, status_code=200)
        msg = Message(type=MessageType.RESPONSE, response=resp)
        extracted = extract_response(msg)
        assert extracted is not None
        assert extracted.request_id == 1

    def test_returns_none_for_request_message(self) -> None:
        msg = Message(type=MessageType.REQUEST, request=Request(id=1))
        assert extract_response(msg) is None

    def test_returns_none_for_keepalive_message(self) -> None:
        msg = Message(type=MessageType.KEEPALIVE)
        assert extract_response(msg) is None


class TestExtractRequest:
    def test_returns_request_for_request_message(self) -> None:
        req = Request(id=5, type=RequestType.PUT_SENSOR_DATA)
        msg = Message(type=MessageType.REQUEST, request=req)
        extracted = extract_request(msg)
        assert extracted is not None
        assert extracted.id == 5

    def test_returns_none_for_response_message(self) -> None:
        msg = Message(type=MessageType.RESPONSE, response=Response(request_id=1))
        assert extract_request(msg) is None

    def test_returns_none_for_keepalive_message(self) -> None:
        msg = Message(type=MessageType.KEEPALIVE)
        assert extract_request(msg) is None


class TestDecodeMessageErrors:
    def test_empty_bytes_decodes_to_default_message(self) -> None:
        # betterproto is lenient — empty bytes decodes to default Message.
        msg = decode_message(b"")
        assert msg.type == MessageType.KEEPALIVE  # default enum value is 0 = KEEPALIVE

    def test_garbage_bytes_does_not_crash(self) -> None:
        # betterproto doesn't raise on most garbage — it silently parses.
        # This test verifies decode_message doesn't crash on arbitrary input.
        msg = decode_message(b"\xff\xfe\xfd\xfc\xfb\xfa")
        assert isinstance(msg, Message)
