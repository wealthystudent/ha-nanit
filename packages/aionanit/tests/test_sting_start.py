"""Wire-format test: async_start_breathing_tracking sends PUT_STING_START."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from aionanit.auth import TokenManager
from aionanit.camera import NanitCamera
from aionanit.proto import RequestType, Response
from aionanit.rest import NanitRestClient
from aionanit.ws.protocol import decode_message


def _make_camera() -> NanitCamera:
    session = MagicMock(spec=aiohttp.ClientSession)
    rest = MagicMock(spec=NanitRestClient)
    tm = MagicMock(spec=TokenManager)
    tm.async_get_access_token = AsyncMock(return_value="test_token")
    tm._expires_at = time.monotonic() + 3600.0
    return NanitCamera(
        uid="cam_uid_1",
        baby_uid="baby_uid_1",
        token_manager=tm,
        rest_client=rest,
        session=session,
    )


@pytest.mark.asyncio
async def test_start_breathing_sends_put_sting_start() -> None:
    cam = _make_camera()
    sent: list[bytes] = []
    cam._transport = MagicMock()
    cam._transport.connected = True
    cam._transport.idle_seconds = 0.0
    resp = Response(request_id=1, request_type=RequestType.PUT_STING_START, status_code=200)

    async def _fake_send(data: bytes) -> None:
        sent.append(data)
        cam._pending.resolve(1, resp)

    cam._transport.async_send = AsyncMock(side_effect=_fake_send)

    await cam.async_start_breathing_tracking()

    assert len(sent) == 1
    msg = decode_message(sent[0])
    assert msg.request.type == RequestType.PUT_STING_START
