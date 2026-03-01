"""Protobuf encode/decode helpers for the Nanit WebSocket protocol."""

from __future__ import annotations

from aionanit.exceptions import NanitProtocolError
from aionanit.proto import (
    Control,
    GetControl,
    GetSensorData,
    GetStatus,
    Message,
    MessageType,
    Request,
    RequestType,
    Response,
    Settings,
    Streaming,
)


def encode_message(msg: Message) -> bytes:
    """Serialize a protobuf Message to bytes."""
    return msg.SerializeToString()


def decode_message(data: bytes) -> Message:
    """Deserialize bytes to a protobuf Message.

    Raises NanitProtocolError on decode failure.
    """
    try:
        return Message.FromString(data)
    except Exception as err:
        raise NanitProtocolError(f"Failed to decode message: {err}") from err


def build_keepalive() -> bytes:
    """Build a serialized KEEPALIVE message."""
    return encode_message(Message(type=MessageType.KEEPALIVE))


def build_request(
    request_id: int,
    request_type: RequestType,
    *,
    streaming: Streaming | None = None,
    settings: Settings | None = None,
    control: Control | None = None,
    get_status: GetStatus | None = None,
    get_sensor_data: GetSensorData | None = None,
    get_control: GetControl | None = None,
) -> bytes:
    """Build a REQUEST message with the given payload.

    Returns serialized bytes ready to send over WebSocket.
    """
    payload = {k: v for k, v in dict(
        streaming=streaming,
        settings=settings,
        control=control,
        get_status=get_status,
        get_sensor_data=get_sensor_data,
        get_control=get_control,
    ).items() if v is not None}

    req = Request(id=request_id, type=request_type, **payload)
    msg = Message(type=MessageType.REQUEST, request=req)
    return encode_message(msg)


def extract_response(msg: Message) -> Response | None:
    """Extract Response from a RESPONSE message, or None if not a response."""
    if msg.type == MessageType.RESPONSE:
        return msg.response
    return None


def extract_request(msg: Message) -> Request | None:
    """Extract Request from a REQUEST message (for push events from camera)."""
    if msg.type == MessageType.REQUEST:
        return msg.request
    return None
