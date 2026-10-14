"""Tests for the local RTMP(S) receiver and its HTTP-FLV fan-out."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import random
import socket
import ssl
import struct
import time
from collections import deque
from pathlib import Path

import aiohttp
import pytest

from aionanit.local_stream import LocalStreamServer, StreamChannel, amf0, channel, flv, rtmp
from aionanit.local_stream.flv import TAG_AUDIO, TAG_SCRIPT, TAG_VIDEO, Tag
from aionanit.local_stream.rtmp import RtmpError, RtmpSession

from .rtmp_publisher import (
    AAC_FRAME,
    AAC_HEADER,
    AVC_HEADER,
    INTERFRAME,
    KEYFRAME,
    Publisher,
    chunk_message,
)


@pytest.fixture(autouse=True)
def _sockets(request: pytest.FixtureRequest) -> None:
    # The Home Assistant test plugin blocks sockets except loopback connects
    # once a test asks for pytest-socket's socket_enabled. Standalone runs
    # without that plugin have sockets open already.
    with contextlib.suppress(pytest.FixtureLookupError):
        request.getfixturevalue("socket_enabled")


KEY = "k3y-for-camera-one"
# The camera's clock: well past the 24 bit field, so extended timestamps.
BASE_TS = 818_622_065


# ----------------------------------------------------------------------
# AMF0
# ----------------------------------------------------------------------


class TestAmf0:
    def test_round_trip(self) -> None:
        values: list[amf0.AmfValue] = [
            "connect",
            1.0,
            {"app": "nanit", "nested": {"a": True}},
            None,
            False,
        ]
        assert amf0.decode_all(amf0.encode_all(*values)) == values

    def test_first_value_length(self) -> None:
        data = amf0.encode_all("@setDataFrame", "onMetaData")
        assert amf0.first_value_length(data) == len(amf0.encode("@setDataFrame"))

    def test_truncated(self) -> None:
        with pytest.raises(amf0.Amf0Error):
            amf0.decode_all(amf0.encode("hello")[:-1])

    def test_depth_limit(self) -> None:
        value: amf0.AmfValue = "leaf"
        for _ in range(amf0.MAX_DEPTH + 2):
            value = {"x": value}
        with pytest.raises(amf0.Amf0Error):
            amf0.decode_all(amf0.encode(value))

    def test_long_string_limit(self) -> None:
        data = bytes([0x0C]) + struct.pack(">I", amf0.MAX_STRING + 1) + b"a" * 10
        with pytest.raises(amf0.Amf0Error):
            amf0.decode_all(data)

    def test_array_limit(self) -> None:
        data = bytes([0x0A]) + struct.pack(">I", amf0.MAX_ELEMENTS + 1)
        with pytest.raises(amf0.Amf0Error):
            amf0.decode_all(data)


# ----------------------------------------------------------------------
# RTMP session (no I/O)
# ----------------------------------------------------------------------


class _Recorder:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.published: list[object] = []
        self.tags: list[Tag] = []
        self.names: list[str] = []

    def authorize(self, name: str) -> object | None:
        self.names.append(name)
        return "token" if self.accept else None

    def session(self) -> RtmpSession:
        return RtmpSession(self.authorize, self.published.append, self.tags.append)


def _handshake() -> bytes:
    return b"\x03" + b"\x01" * rtmp.HANDSHAKE_SIZE + b"\x02" * rtmp.HANDSHAKE_SIZE


def _publish_stream(chunk_size: int = 128) -> bytes:
    def cmd(*values: amf0.AmfValue, stream_id: int = 0) -> bytes:
        return chunk_message(3, 20, stream_id, 0, amf0.encode_all(*values), chunk_size)

    return (
        cmd("connect", 1, {"app": "nanit"})
        + cmd("createStream", 2, None)
        + cmd("publish", 3, None, KEY + "?x=1", "live", stream_id=1)
    )


def _media_stream() -> bytes:
    data = amf0.encode_all("@setDataFrame", "onMetaData", {"width": 1920.0})
    return (
        chunk_message(4, 18, 1, 0, data, 128)
        + chunk_message(6, 9, 1, BASE_TS, AVC_HEADER, 128)
        + chunk_message(4, 8, 1, BASE_TS, AAC_HEADER, 128)
        + chunk_message(6, 9, 1, BASE_TS, KEYFRAME, 128)
        + chunk_message(4, 8, 1, BASE_TS + 23, AAC_FRAME, 128)
        + chunk_message(6, 9, 1, BASE_TS + 100, INTERFRAME, 128)
    )


class TestRtmpSession:
    def test_handshake_echoes_c1(self) -> None:
        session = _Recorder().session()
        out = session.feed(_handshake())
        assert out[0] == 3
        assert out[1 + rtmp.HANDSHAKE_SIZE :] == b"\x01" * rtmp.HANDSHAKE_SIZE

    @pytest.mark.parametrize("step", [1, 7, 128, 100_000])
    def test_publish_and_media_in_any_split(self, step: int) -> None:
        rec = _Recorder()
        session = rec.session()
        data = _handshake() + _publish_stream() + _media_stream()
        out = b""
        for i in range(0, len(data), step):
            out += session.feed(data[i : i + step])
        assert rec.names == [KEY]
        assert rec.published == ["token"]
        assert session.app == "nanit"
        kinds = [(t.kind, t.timestamp) for t in rec.tags]
        assert kinds == [
            (TAG_SCRIPT, 0),
            (TAG_VIDEO, BASE_TS),
            (TAG_AUDIO, BASE_TS),
            (TAG_VIDEO, BASE_TS),
            (TAG_AUDIO, BASE_TS + 23),
            (TAG_VIDEO, BASE_TS + 100),
        ]
        # @setDataFrame is stripped, onMetaData stays.
        assert amf0.decode_all(rec.tags[0].body)[0] == "onMetaData"
        assert rec.tags[3].body == KEYFRAME
        assert b"NetConnection.Connect.Success" in out
        assert b"NetStream.Publish.Start" in out

    def test_delta_timestamps(self) -> None:
        rec = _Recorder()
        session = rec.session()
        session.feed(_handshake() + _publish_stream())
        first = chunk_message(6, 9, 1, 1000, INTERFRAME, 4096)
        # Format 1 (delta 40, same csid) then format 3 (repeat the delta).
        fmt1 = bytes([0x40 | 6]) + (40).to_bytes(3, "big") + len(INTERFRAME).to_bytes(3, "big")
        fmt1 += bytes([9])
        session.feed(first + fmt1 + INTERFRAME + bytes([0xC0 | 6]) + INTERFRAME)
        assert [t.timestamp for t in rec.tags] == [1000, 1040, 1080]

    def test_peer_chunk_size_change(self) -> None:
        rec = _Recorder()
        session = rec.session()
        set_size = chunk_message(2, 1, 0, 0, struct.pack(">I", 4096), 128)
        session.feed(_handshake() + set_size + _publish_stream(4096))
        session.feed(chunk_message(6, 9, 1, 5, KEYFRAME, 4096))
        assert rec.tags[-1].body == KEYFRAME

    def test_refused_publish(self) -> None:
        session = _Recorder(accept=False).session()
        with pytest.raises(RtmpError):
            session.feed(_handshake() + _publish_stream())

    def test_bad_version(self) -> None:
        with pytest.raises(RtmpError):
            _Recorder().session().feed(b"\x06" + b"\x00" * rtmp.HANDSHAKE_SIZE)

    def test_oversize_message(self) -> None:
        session = _Recorder().session()
        session.feed(_handshake())
        header = bytes([3]) + b"\x00\x00\x00" + (rtmp.MAX_MESSAGE_SIZE + 1).to_bytes(3, "big")
        header += bytes([20]) + b"\x00\x00\x00\x00"
        with pytest.raises(RtmpError):
            session.feed(header + b"\x00" * 10)

    def test_too_many_chunk_streams(self) -> None:
        session = _Recorder().session()
        session.feed(_handshake())
        data = b"".join(
            bytes([1]) + bytes([cs - 64, 0]) + chunk_message(0, 5, 0, 0, b"\x00" * 4, 128)[1:]
            for cs in range(64, 64 + rtmp.MAX_CHUNK_STREAMS + 1)
        )
        with pytest.raises(RtmpError):
            session.feed(data)

    def test_continuation_without_header(self) -> None:
        session = _Recorder().session()
        session.feed(_handshake())
        with pytest.raises(RtmpError):
            session.feed(bytes([0xC0 | 9]) + b"\x00" * 8)

    def test_pre_publish_byte_cap(self) -> None:
        session = _Recorder().session()
        session.feed(_handshake())
        filler = chunk_message(3, 18, 0, 0, b"\x05" * 1000, 128)
        with pytest.raises(RtmpError):
            for _ in range(rtmp.MAX_PRE_PUBLISH_BYTES // len(filler) + 2):
                session.feed(filler)

    def test_media_ignored_before_publish(self) -> None:
        rec = _Recorder()
        session = rec.session()
        session.feed(_handshake() + chunk_message(6, 9, 1, 5, KEYFRAME, 128))
        assert rec.tags == []

    def test_acknowledges_peer_window(self) -> None:
        rec = _Recorder()
        session = rec.session()
        window = chunk_message(2, 5, 0, 0, struct.pack(">I", 5000), 128)
        session.feed(_handshake() + window + _publish_stream())
        out = session.feed(chunk_message(6, 9, 1, 5, b"\x27\x01" + b"\x00" * 6000, 128))
        assert b"\x03" in out  # an Acknowledgement (type 3) went out

    def test_unpublish_closes(self) -> None:
        session = _Recorder().session()
        session.feed(_handshake() + _publish_stream())
        session.feed(chunk_message(3, 20, 1, 0, amf0.encode_all("deleteStream", 6, None, 1), 128))
        assert session.closed

    def test_fuzz_never_escapes_rtmp_error(self) -> None:
        rng = random.Random(1234)
        valid = _handshake() + _publish_stream() + _media_stream()
        for _ in range(400):
            data = bytearray(valid)
            for _ in range(rng.randint(1, 12)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            if rng.random() < 0.3:
                data += bytes(rng.randrange(256) for _ in range(rng.randint(1, 2000)))
            session = _Recorder().session()
            try:
                for i in range(0, len(data), rng.randint(1, 3000)):
                    session.feed(bytes(data[i : i + 3000]))
            except RtmpError:
                pass


# ----------------------------------------------------------------------
# FLV
# ----------------------------------------------------------------------


def test_flv_tag_encoding_rebases_timestamps() -> None:
    tag = Tag(TAG_VIDEO, BASE_TS + 40, KEYFRAME)
    data = tag.encode(BASE_TS)
    assert data[0] == TAG_VIDEO
    assert int.from_bytes(data[1:4], "big") == len(KEYFRAME)
    assert int.from_bytes(data[4:7], "big") == 40
    assert data[7] == 0
    assert struct.unpack(">I", data[-4:])[0] == 11 + len(KEYFRAME)
    assert tag.is_keyframe
    assert Tag(TAG_VIDEO, 0, AVC_HEADER).is_avc_header
    assert Tag(TAG_AUDIO, 0, AAC_HEADER).is_aac_header


def test_flv_file_header_flags_audio_and_video() -> None:
    assert flv.FILE_HEADER[:4] == b"FLV\x01"
    assert flv.FILE_HEADER[4] == 0x05
    assert flv.FILE_HEADER[9:13] == b"\x00\x00\x00\x00"


# ----------------------------------------------------------------------
# Server, end to end
# ----------------------------------------------------------------------


def _parse_flv(data: bytes) -> list[tuple[int, int, bytes]]:
    assert data[:13] == flv.FILE_HEADER
    pos = 13
    tags = []
    while pos + 11 <= len(data):
        kind = data[pos]
        size = int.from_bytes(data[pos + 1 : pos + 4], "big")
        ts = int.from_bytes(data[pos + 4 : pos + 7], "big") | data[pos + 7] << 24
        if pos + 11 + size + 4 > len(data):
            break
        tags.append((kind, ts, data[pos + 11 : pos + 11 + size]))
        pos += 11 + size + 4
    return tags


async def _start_server(tls: ssl.SSLContext | None = None) -> LocalStreamServer:
    server = LocalStreamServer(port=0, host="127.0.0.1", ssl_context=tls)
    await server.async_start()
    return server


def _wire_start(
    server: LocalStreamServer, ch: StreamChannel, *, tls: bool = False
) -> list[Publisher]:
    """Make the channel's start callback launch a publisher, like a camera would."""
    publishers: list[Publisher] = []

    async def start() -> bool:
        pub = Publisher(server.rtmp_port, KEY, tls=tls)
        publishers.append(pub)
        await pub.publish()
        pub.metadata()
        pub.media(9, BASE_TS, AVC_HEADER)
        pub.media(8, BASE_TS, AAC_HEADER)
        pub.media(9, BASE_TS, KEYFRAME)
        pub.media(8, BASE_TS + 23, AAC_FRAME)
        pub.media(9, BASE_TS + 100, INTERFRAME)
        return await ch.wait_for_publish(5)

    ch.start_callback = start
    return publishers


async def _read_some(url: str, until: int, timeout: float = 5) -> bytes:
    data = b""
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "video/x-flv"
        async with asyncio.timeout(timeout):
            async for chunk in resp.content.iter_any():
                data += chunk
                if len(_parse_flv(data)) >= until:
                    break
    return data


class TestServer:
    async def test_first_subscriber_starts_camera_and_gets_flv(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            consumers: list[int] = []
            ch.on_consumers_changed = consumers.append
            data = await _read_some(server.flv_url("cam1"), until=6)
            tags = _parse_flv(data)
            assert [(k, ts) for k, ts, _ in tags[:6]] == [
                (TAG_SCRIPT, 0),
                (TAG_VIDEO, 0),
                (TAG_AUDIO, 0),
                (TAG_VIDEO, 0),
                (TAG_AUDIO, 23),
                (TAG_VIDEO, 100),
            ]
            assert tags[1][2] == AVC_HEADER
            assert tags[3][2] == KEYFRAME
            assert ch.publishing
            assert ch.publisher_peer == "127.0.0.1"
            assert len(publishers) == 1
            assert consumers[0] == 1
        finally:
            await server.async_stop()

    async def test_concurrent_subscribers_share_one_start(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            url = server.flv_url("cam1")
            await asyncio.gather(_read_some(url, 4), _read_some(url, 4))
            assert len(publishers) == 1
        finally:
            await server.async_stop()

    async def test_late_joiner_starts_at_cached_keyframe(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            await _read_some(server.flv_url("cam1"), 6)
            # A second viewer joins while the camera is live: no new start,
            # and its media starts at the cached keyframe.
            data = await _read_some(server.flv_url("cam1"), 6)
            tags = _parse_flv(data)
            media = [t for t in tags[1:] if not (t[0] == TAG_VIDEO and t[2] == AVC_HEADER)]
            media = [t for t in media if not (t[0] == TAG_AUDIO and t[2] == AAC_HEADER)]
            assert media[0][2] == KEYFRAME
            assert media[0][1] == 0
            assert len(publishers) == 1
        finally:
            await server.async_stop()

    async def test_wrong_key_is_refused_and_counted(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            pub = Publisher(server.rtmp_port, "not-the-key")
            await pub.publish()
            assert pub.reader is not None
            async with asyncio.timeout(5):
                await pub.reader.read()  # server closes the connection
            await pub.close()
            assert not ch.publishing
            assert server.connection_attempts == 1
            assert server.protocol_failures == 1
        finally:
            await server.async_stop()

    async def test_wrong_app_is_refused(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            pub = Publisher(server.rtmp_port, KEY, app="live")
            await pub.publish()
            assert pub.reader is not None
            async with asyncio.timeout(5):
                await pub.reader.read()
            await pub.close()
            assert not ch.publishing
        finally:
            await server.async_stop()

    async def test_bad_secret_and_unknown_camera(self) -> None:
        server = await _start_server()
        try:
            server.register("cam1", KEY)
            base = f"http://127.0.0.1:{server.http_port}/nanit/"
            async with aiohttp.ClientSession() as session:
                async with session.get(base + "cam1.flv?k=wrong") as resp:
                    assert resp.status == 404
                url = server.flv_url("cam1").replace("cam1", "nope")
                async with session.get(url) as resp:
                    assert resp.status == 404
        finally:
            await server.async_stop()

    async def test_not_accepting_answers_503_without_starting(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            ch.accepting = False
            async with (
                aiohttp.ClientSession() as session,
                session.get(server.flv_url("cam1")) as resp,
            ):
                assert resp.status == 503
            assert publishers == []
        finally:
            await server.async_stop()

    async def test_failed_start_ends_the_response(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)

            async def no_camera() -> bool:
                return False

            ch.start_callback = no_camera
            async with (
                aiohttp.ClientSession() as session,
                session.get(server.flv_url("cam1")) as resp,
            ):
                assert resp.status == 200
                body = await resp.read()
            assert body == b""
            assert ch.consumer_count == 0
        finally:
            await server.async_stop()

    async def test_publisher_loss_closes_subscribers_after_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(channel, "PUBLISHER_GRACE", 0.1)
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            async with (
                aiohttp.ClientSession() as session,
                session.get(server.flv_url("cam1")) as resp,
            ):
                await resp.content.readexactly(13)
                await publishers[0].close()
                async with asyncio.timeout(5):
                    await resp.read()  # ends once the grace expires
            assert ch.consumer_count == 0
            assert not ch.publishing
        finally:
            await server.async_stop()

    async def test_live_publisher_is_not_replaced(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            first = Publisher(server.rtmp_port, KEY)
            await first.publish()
            assert await ch.wait_for_publish(5)
            first.media(9, BASE_TS, KEYFRAME)
            await asyncio.sleep(0.05)
            second = Publisher(server.rtmp_port, KEY)
            await second.publish()
            assert second.reader is not None
            async with asyncio.timeout(5):
                await second.reader.read()  # refused while the first is live
            assert ch.publish_count == 1
            await first.close()
            await second.close()
        finally:
            await server.async_stop()

    async def test_silent_publisher_is_replaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(channel, "REPLACE_AFTER_SILENCE", 0.05)
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            first = Publisher(server.rtmp_port, KEY)
            await first.publish()
            assert await ch.wait_for_publish(5)
            await asyncio.sleep(0.1)
            second = Publisher(server.rtmp_port, KEY)
            await second.publish()
            async with asyncio.timeout(5):
                while ch.publish_count < 2:
                    await asyncio.sleep(0.01)
            assert first.reader is not None
            async with asyncio.timeout(5):
                await first.reader.read()  # the silent one was aborted
            await first.close()
            await second.close()
        finally:
            await server.async_stop()

    async def test_stop_with_live_publisher_is_fast(self) -> None:
        server = await _start_server()
        ch = server.register("cam1", KEY)
        pub = Publisher(server.rtmp_port, KEY)
        await pub.publish()
        assert await ch.wait_for_publish(5)
        started = time.monotonic()
        await server.async_stop()
        assert time.monotonic() - started < 1.0
        await pub.close()

    async def test_publish_url_brackets_ipv6(self) -> None:
        server = await _start_server()
        try:
            server.register("cam1", KEY)
            assert server.publish_url("192.168.1.10", "cam1") == (
                f"rtmp://192.168.1.10:{server.rtmp_port}/nanit/{KEY}"
            )
            assert server.publish_url("fd00::5", "cam1").startswith("rtmp://[fd00::5]:")
        finally:
            await server.async_stop()

    async def test_next_port_when_taken(self) -> None:
        first = await _start_server()
        try:
            second = LocalStreamServer(port=first.rtmp_port, host="127.0.0.1", port_attempts=5)
            await second.async_start()
            assert second.rtmp_port != first.rtmp_port
            await second.async_stop()
            third = LocalStreamServer(port=first.rtmp_port, host="127.0.0.1")
            with pytest.raises(OSError):
                await third.async_start()
        finally:
            await first.async_stop()

    async def test_audio_only_subscription(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            _wire_start(server, ch)
            assert await ch.ensure_publishing()
            sub = ch.subscribe(audio_only=True)
            pub_tags = []
            async with asyncio.timeout(5):
                while (tag := await sub.get()) is not None:
                    pub_tags.append(tag)
                    if len(pub_tags) == 1:
                        break
            assert pub_tags[0].kind == TAG_AUDIO
            assert pub_tags[0].body == AAC_FRAME
            sub.close()
        finally:
            await server.async_stop()


# ----------------------------------------------------------------------
# TLS
# ----------------------------------------------------------------------


def _self_signed(tmp_path: Path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


class TestTls:
    async def test_rtmps_end_to_end(self, tmp_path: Path) -> None:
        server = await _start_server(_self_signed(tmp_path))
        try:
            ch = server.register("cam1", KEY)
            _wire_start(server, ch, tls=True)
            data = await _read_some(server.flv_url("cam1"), until=4)
            assert _parse_flv(data)[3][2] == KEYFRAME
            assert server.publish_url("10.0.0.1", "cam1").startswith("rtmps://")
        finally:
            await server.async_stop()

    async def test_plain_client_against_tls_counts_a_protocol_failure(self, tmp_path: Path) -> None:
        server = await _start_server(_self_signed(tmp_path))
        try:
            server.register("cam1", KEY)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.rtmp_port)
            writer.write(b"\x03" + b"\x00" * rtmp.HANDSHAKE_SIZE)
            async with asyncio.timeout(15):
                await reader.read()
            writer.close()
            async with asyncio.timeout(5):
                while server.protocol_failures == 0:
                    await asyncio.sleep(0.01)
            assert server.connection_attempts == 1
        finally:
            await server.async_stop()


# ----------------------------------------------------------------------
# FFmpeg's own chunking, ported from FFmpeg 4.1 rtmppkt.c / rtmpproto.c
# ----------------------------------------------------------------------


class _FFWriter:
    """Python port of FFmpeg 4.1 ff_rtmp_packet_write."""

    def __init__(self, chunk_size: int = 128) -> None:
        self.prev: dict[int, dict[str, int]] = {}
        self.chunk_size = chunk_size

    def write(
        self, ch: int, kind: int, ts: int, extra: int, data: bytes, *, force_full: bool = False
    ) -> bytes:
        if force_full and ch in self.prev:
            self.prev[ch]["channel_id"] = 0
        p = self.prev.get(
            ch, {"channel_id": 0, "extra": 0, "timestamp": 0, "type": 0, "size": 0, "ts_field": 0}
        )
        use_delta = bool(p["channel_id"]) and extra == p["extra"] and ts >= p["timestamp"]
        timestamp = ts - p["timestamp"] if use_delta else ts
        ts_field = 0xFFFFFF if timestamp >= 0xFFFFFF else timestamp
        mode = 0
        if use_delta:
            if kind == p["type"] and len(data) == p["size"]:
                mode = 3 if ts_field == p["ts_field"] else 2
            else:
                mode = 1
        out = bytearray([ch | (mode << 6)])
        if mode != 3:
            out += ts_field.to_bytes(3, "big")
            if mode != 2:
                out += len(data).to_bytes(3, "big") + bytes([kind])
                if mode == 0:
                    out += struct.pack("<I", extra)
        if ts_field == 0xFFFFFF:
            out += struct.pack(">I", timestamp)
        self.prev[ch] = {
            "channel_id": ch,
            "type": kind,
            "size": len(data),
            "timestamp": ts,
            "ts_field": ts_field,
            "extra": extra,
        }
        off = 0
        while off < len(data):
            n = min(self.chunk_size, len(data) - off)
            out += data[off : off + n]
            off += n
            if off < len(data):
                out += bytes([0xC0 | ch])
                if ts_field == 0xFFFFFF:
                    out += struct.pack(">I", timestamp)
        return bytes(out)

    def media(self, kind: int, ts: int, body: bytes) -> bytes:
        """Mimic rtmp_write's channel choice and forced full header."""
        ch = 6 if kind == TAG_VIDEO else 4
        force = (kind in (TAG_AUDIO, TAG_VIDEO) and ts == 0) or kind == TAG_SCRIPT
        return self.write(ch, kind, ts, 1, body, force_full=force)


class TestFfmpegShapedFeed:
    def test_camera_sequence_through_ffmpegs_writer(self) -> None:
        rec = _Recorder()
        session = rec.session()
        session.feed(_handshake() + _publish_stream())
        # FFmpeg echoes our SetChunkSize(4096) and switches to it.
        session.feed(chunk_message(2, 1, 0, 0, struct.pack(">I", 4096), 128))
        writer = _FFWriter(4096)
        meta = amf0.encode_all("@setDataFrame", "onMetaData", {"width": 1920.0})
        big_keyframe = bytes([0x17, 1, 0, 0, 0]) + b"\x00" * 20000
        seq = [
            (TAG_SCRIPT, 0, meta),
            (TAG_VIDEO, 0, AVC_HEADER),
            (TAG_AUDIO, 0, AAC_HEADER),
            (TAG_VIDEO, BASE_TS, big_keyframe),
        ]
        t_audio = t_video = BASE_TS
        for i in range(30):
            t_audio += 23 if i % 3 else 24
            seq.append((TAG_AUDIO, t_audio, AAC_FRAME))
            if i % 4 == 0:
                t_video += 100
                seq.append((TAG_VIDEO, t_video, bytes([0x27, 1, 0, 0, 0]) + b"\x01" * 5000))
        seq.sort(key=lambda item: item[1])
        session.feed(b"".join(writer.media(k, t, b) for k, t, b in seq))
        got = [(t.kind, t.timestamp, len(t.body)) for t in rec.tags]
        want = [(k, t, len(b) - (16 if k == TAG_SCRIPT else 0)) for k, t, b in seq]
        assert got == want

    def test_format3_after_format0_reuses_the_timestamp_as_delta(self) -> None:
        rec = _Recorder()
        session = rec.session()
        session.feed(_handshake() + _publish_stream())
        writer = _FFWriter(128)
        data = writer.write(4, TAG_AUDIO, 23, 1, AAC_FRAME) + writer.write(
            4, TAG_AUDIO, 46, 1, AAC_FRAME
        )
        assert data[12 + len(AAC_FRAME)] >> 6 == 3  # FFmpeg emitted format 3
        session.feed(data)
        assert [t.timestamp for t in rec.tags] == [23, 46]

    def test_size_change_mid_message_is_refused(self) -> None:
        session = _Recorder().session()
        session.feed(_handshake() + _publish_stream())
        first = bytes([6]) + (5).to_bytes(3, "big") + (1000).to_bytes(3, "big") + bytes([9])
        first += struct.pack("<I", 1) + b"\x17\x01" + b"\x00" * 126
        bigger = bytes([0x40 | 6]) + (0).to_bytes(3, "big")
        bigger += (2_000_000).to_bytes(3, "big") + bytes([9]) + b"\x00" * 128
        with pytest.raises(RtmpError):
            session.feed(first + bigger)

    def test_media_on_another_stream_is_ignored(self) -> None:
        rec = _Recorder()
        session = rec.session()
        session.feed(_handshake() + _publish_stream())
        session.feed(chunk_message(6, 9, 2, 5, KEYFRAME, 128))
        assert rec.tags == []


# ----------------------------------------------------------------------
# Channel: GOP cache, gating, timestamps
# ----------------------------------------------------------------------


def _live_channel() -> StreamChannel:
    ch = StreamChannel("cam1", KEY)
    ch.publish_started(1, "10.0.0.5", lambda: None)
    ch.tag_received(1, Tag(TAG_VIDEO, 0, AVC_HEADER))
    return ch


class TestChannel:
    async def test_mid_gop_subscriber_starts_at_the_next_keyframe(self) -> None:
        ch = StreamChannel("cam1", KEY)
        ch.publish_started(1, None, lambda: None)
        sub = ch.subscribe()  # nothing cached yet
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS + 100, INTERFRAME))
        ch.tag_received(1, Tag(TAG_AUDIO, BASE_TS + 110, AAC_FRAME))
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS + 200, KEYFRAME))
        first = await sub.get()
        assert first is not None
        assert first.body == KEYFRAME

    async def test_second_keyframe_resets_the_cache(self) -> None:
        ch = _live_channel()
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS, KEYFRAME))
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS + 100, INTERFRAME))
        second = KEYFRAME + b"\x09"
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS + 200, second))
        sub = ch.subscribe()
        first = await sub.get()
        assert first is not None
        assert first.body == second
        assert sub._queue == deque()

    async def test_old_gop_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(channel, "MAX_GOP_MILLIS", 150)
        ch = _live_channel()
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS, KEYFRAME))
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS + 200, INTERFRAME))
        sub = ch.subscribe()
        assert not sub._queue

    async def test_audio_just_before_the_keyframe_keeps_the_gop(self) -> None:
        ch = _live_channel()
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS, KEYFRAME))
        ch.tag_received(1, Tag(TAG_AUDIO, BASE_TS - 10, AAC_FRAME))
        sub = ch.subscribe()
        assert len(sub._queue) == 2
        assert Tag(TAG_AUDIO, BASE_TS - 10, AAC_FRAME).encode(BASE_TS)[4:8] == b"\x00" * 4

    async def test_slow_subscriber_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(channel, "MAX_SUBSCRIBER_BYTES", 1000)
        ch = _live_channel()
        sub = ch.subscribe()
        for i in range(10):
            ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS + i, KEYFRAME))
        assert sub.closed
        assert ch.consumer_count == 0

    async def test_new_publisher_closes_existing_subscribers(self) -> None:
        ch = _live_channel()
        sub = ch.subscribe()
        ch.publish_started(2, None, lambda: None)
        assert sub.closed

    async def test_late_aac_header_reaches_existing_subscribers(self) -> None:
        ch = _live_channel()
        ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS, KEYFRAME))
        sub = ch.subscribe()
        ch.tag_received(1, Tag(TAG_AUDIO, 0, AAC_HEADER))
        bodies = [(await sub.get()) for _ in range(2)]
        assert [t.body for t in bodies if t is not None] == [KEYFRAME, AAC_HEADER]

    async def test_close_during_start_returns_false(self) -> None:
        ch = StreamChannel("cam1", KEY)
        started = asyncio.Event()

        async def slow() -> bool:
            started.set()
            await asyncio.sleep(30)
            return True

        ch.start_callback = slow
        waiter = asyncio.ensure_future(ch.ensure_publishing())
        await started.wait()
        ch.close()
        assert await waiter is False


# ----------------------------------------------------------------------
# Server: caps, timeouts, gates
# ----------------------------------------------------------------------


async def _idle_connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


async def _closed_by_server(reader: asyncio.StreamReader) -> bool:
    """Return True once the server closes, whatever it sent before."""
    try:
        async with asyncio.timeout(5):
            await reader.read()  # reads to EOF
    except TimeoutError:
        return False
    except ConnectionError:
        return True
    return True


class TestServerLimits:
    async def test_same_peer_evicts_its_oldest_pending(self) -> None:
        server = await _start_server()
        try:
            r1, w1 = await _idle_connect(server.rtmp_port)
            _r2, w2 = await _idle_connect(server.rtmp_port)
            _r3, w3 = await _idle_connect(server.rtmp_port)
            assert await _closed_by_server(r1)
            assert len(server._pending) == 2
            for w in (w1, w2, w3):
                w.close()
        finally:
            await server.async_stop()

    async def test_full_pending_cap_evicts_and_lets_the_camera_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aionanit.local_stream import server as server_mod

        monkeypatch.setattr(server_mod, "MAX_PENDING_PER_PEER", 100)
        monkeypatch.setattr(server_mod, "MAX_PENDING_CONNECTIONS", 3)
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            idle = [await _idle_connect(server.rtmp_port) for _ in range(3)]
            pub = Publisher(server.rtmp_port, KEY)
            await pub.publish()
            assert await ch.wait_for_publish(5)
            assert await _closed_by_server(idle[0][0])
            for _, w in idle:
                w.close()
            await pub.close()
        finally:
            await server.async_stop()

    async def test_tls_handshakes_count_against_the_cap(self, tmp_path: Path) -> None:
        from aionanit.local_stream import server as server_mod

        server = await _start_server(_self_signed(tmp_path))
        try:
            conns = [await _idle_connect(server.rtmp_port) for _ in range(12)]
            await asyncio.sleep(0.1)
            assert len(server._pending) <= server_mod.MAX_PENDING_PER_PEER
            for _, w in conns:
                w.close()
        finally:
            await server.async_stop()

    async def test_idle_connection_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aionanit.local_stream import server as server_mod

        monkeypatch.setattr(server_mod, "PRE_PUBLISH_TIMEOUT", 0.2)
        server = await _start_server()
        try:
            reader, writer = await _idle_connect(server.rtmp_port)
            assert await _closed_by_server(reader)
            assert server.protocol_failures == 1
            writer.close()
        finally:
            await server.async_stop()

    async def test_silent_publisher_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aionanit.local_stream import server as server_mod

        monkeypatch.setattr(server_mod, "IDLE_TIMEOUT", 0.3)
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            pub = Publisher(server.rtmp_port, KEY)
            await pub.publish()
            assert await ch.wait_for_publish(5)
            assert pub.reader is not None
            assert await _closed_by_server(pub.reader)
            await pub.close()
        finally:
            await server.async_stop()

    async def test_head_does_not_start_the_camera(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            async with aiohttp.ClientSession() as session:
                async with session.head(server.flv_url("cam1")) as resp:
                    assert resp.status == 405
            assert publishers == []
        finally:
            await server.async_stop()

    async def test_head_of_stream_waits_for_the_avc_header(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            pub = Publisher(server.rtmp_port, KEY)

            async def start() -> bool:
                await pub.publish()
                pub.media(9, BASE_TS, KEYFRAME)  # media before any header
                return await ch.wait_for_publish(5)

            ch.start_callback = start
            async with aiohttp.ClientSession() as session:
                async with session.get(server.flv_url("cam1")) as resp:
                    with pytest.raises(TimeoutError):
                        async with asyncio.timeout(0.5):
                            await resp.content.readexactly(1)
                    pub.media(9, BASE_TS, AVC_HEADER)
                    pub.media(9, BASE_TS + 100, KEYFRAME)
                    async with asyncio.timeout(5):
                        head = await resp.content.readexactly(13 + 11 + len(AVC_HEADER) + 4)
            assert head.startswith(flv.FILE_HEADER)
            assert head[13 + 11 : 13 + 11 + len(AVC_HEADER)] == AVC_HEADER
            await pub.close()
        finally:
            await server.async_stop()

    async def test_stop_while_waiting_for_headers_is_fast(self) -> None:
        server = await _start_server()
        ch = server.register("cam1", KEY)
        pub = Publisher(server.rtmp_port, KEY)

        async def start() -> bool:
            await pub.publish()
            return await ch.wait_for_publish(5)

        ch.start_callback = start
        session = aiohttp.ClientSession()
        try:
            resp = await session.get(server.flv_url("cam1"))
            await asyncio.sleep(0.2)
            started = time.monotonic()
            await server.async_stop()
            assert time.monotonic() - started < 1.0
            resp.close()
        finally:
            await session.close()
            await pub.close()


# ----------------------------------------------------------------------
# A real demuxer reads the FLV (PyAV, the library HA's stream worker uses)
# ----------------------------------------------------------------------


def _demux(url: str, packets: int) -> tuple[list[str], list[tuple[str, int]]]:
    import av

    container = av.open(url, timeout=10)
    try:
        codecs = sorted(stream.codec_context.name for stream in container.streams)
        seen: list[tuple[str, int]] = []
        for packet in container.demux():
            if packet.dts is None:
                continue
            seen.append((packet.stream.type, int(packet.dts)))
            if len(seen) >= packets:
                break
        return codecs, seen
    finally:
        container.close()


class TestRealDemuxer:
    async def test_pyav_reads_cold_start_and_late_join(self) -> None:
        pytest.importorskip("av")
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            loop = asyncio.get_running_loop()
            url = server.flv_url("cam1")

            async def feed() -> None:
                # Keep media flowing so both readers get packets.
                while not publishers:
                    await asyncio.sleep(0.01)
                pub = publishers[0]
                for i in range(1, 40):
                    pub.media(8, BASE_TS + 23 * i, AAC_FRAME)
                    if i % 4 == 0:
                        pub.media(9, BASE_TS + 100 * (i // 4), INTERFRAME)
                    await asyncio.sleep(0.02)

            feeder = asyncio.ensure_future(feed())
            codecs, seen = await loop.run_in_executor(None, _demux, url, 6)
            assert codecs == ["aac", "h264"]
            video = [dts for kind, dts in seen if kind == "video"]
            assert video == sorted(video)
            # A second reader joins live, primed from the GOP cache.
            codecs, seen = await loop.run_in_executor(None, _demux, url, 3)
            assert codecs == ["aac", "h264"]
            assert seen[0][0] == "video"
            await feeder
            assert len(publishers) == 1
        finally:
            await server.async_stop()


# ----------------------------------------------------------------------
# Round-2 survivors
# ----------------------------------------------------------------------


def test_ts_since_boundary() -> None:
    assert flv.ts_since(1000 + (1 << 31) - 1, 1000) == (1 << 31) - 1
    assert flv.ts_since(1000 + (1 << 31), 1000) == 0


def test_type_change_mid_message_is_refused() -> None:
    session = _Recorder().session()
    session.feed(_handshake() + _publish_stream())
    first = bytes([6]) + (5).to_bytes(3, "big") + (300).to_bytes(3, "big") + bytes([9])
    first += struct.pack("<I", 1) + b"\x17\x01" + b"\x00" * 126
    other = bytes([0x40 | 6]) + (0).to_bytes(3, "big") + (300).to_bytes(3, "big") + bytes([8])
    with pytest.raises(RtmpError):
        session.feed(first + other + b"\x00" * 128)


def test_header_fields_are_taken_only_when_a_message_starts() -> None:
    rec = _Recorder()
    session = rec.session()
    session.feed(_handshake() + _publish_stream())
    body = b"\x27\x01" + b"\x00" * 198  # 200 bytes, two chunks at 128
    start = bytes([6]) + (1000).to_bytes(3, "big") + len(body).to_bytes(3, "big") + bytes([9])
    start += struct.pack("<I", 1) + body[:128]
    # A format 2 chunk mid-message carrying a different timestamp delta.
    middle = bytes([0x80 | 6]) + (77).to_bytes(3, "big") + body[128:]
    session.feed(start + middle)
    session.feed(chunk_message(6, 9, 1, 2000, INTERFRAME, 128))
    assert [t.timestamp for t in rec.tags] == [1000, 2000]
    assert rec.tags[0].body == body


async def test_close_wakes_publish_waiters() -> None:
    ch = StreamChannel("cam1", KEY)
    waiter = asyncio.ensure_future(ch.wait_for_publish(30))
    await asyncio.sleep(0)
    ch.close()
    async with asyncio.timeout(1):
        assert await waiter is False


async def test_close_makes_header_waiters_give_up() -> None:
    ch = _live_channel()
    ch.close()
    assert await ch.wait_for_headers(1) is False


async def test_subscription_iterates_until_closed() -> None:
    ch = _live_channel()
    ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS, KEYFRAME))
    sub = ch.subscribe()
    ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS + 100, INTERFRAME))
    seen = []

    async def collect() -> None:
        async for tag in sub:
            seen.append(tag.body)

    task = asyncio.ensure_future(collect())
    await asyncio.sleep(0.01)
    sub.close()
    async with asyncio.timeout(1):
        await task
    assert seen == [KEYFRAME, INTERFRAME]


async def test_unchanged_header_is_not_resent_but_a_change_is() -> None:
    ch = _live_channel()
    ch.tag_received(1, Tag(TAG_AUDIO, 0, AAC_HEADER))
    ch.tag_received(1, Tag(TAG_VIDEO, BASE_TS, KEYFRAME))
    sub = ch.subscribe()
    ch.tag_received(1, Tag(TAG_AUDIO, 0, AAC_HEADER))  # same config again
    ch.tag_received(1, Tag(TAG_VIDEO, 0, AVC_HEADER))  # same config again
    assert [t.body for t in sub._queue] == [KEYFRAME]
    changed = AAC_HEADER[:-1] + b"\x10"
    ch.tag_received(1, Tag(TAG_AUDIO, 0, changed))
    assert [t.body for t in sub._queue] == [KEYFRAME, changed]


class TestServerRound2:
    async def test_connections_and_failures_are_counted_per_peer(self) -> None:
        server = await _start_server()
        try:
            server.register("cam1", KEY)
            pub = Publisher(server.rtmp_port, "wrong")
            await pub.publish()
            assert pub.reader is not None
            assert await _closed_by_server(pub.reader)
            await pub.close()
            assert server.attempts_by_peer == {"127.0.0.1": 1}
            assert server.failures_by_peer == {"127.0.0.1": 1}
        finally:
            await server.async_stop()

    async def test_tracked_peers_are_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aionanit.local_stream import server as server_mod

        monkeypatch.setattr(server_mod, "MAX_TRACKED_PEERS", 3)
        server = LocalStreamServer(port=0)
        for i in range(10):
            server.record_failure(f"10.0.0.{i}")
        assert len(server.failures_by_peer) == 3
        assert server.protocol_failures == 10

    async def test_eviction_mid_tls_closes_the_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aionanit.local_stream import server as server_mod

        monkeypatch.setattr(server_mod, "MAX_PENDING_PER_PEER", 1)
        server = await _start_server(_self_signed(tmp_path))
        try:
            first_reader, first_writer = await _idle_connect(server.rtmp_port)
            _r, second_writer = await _idle_connect(server.rtmp_port)
            assert await _closed_by_server(first_reader)
            first_writer.close()
            second_writer.close()
        finally:
            await server.async_stop()

    async def test_stalled_reader_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aionanit.local_stream import server as server_mod

        monkeypatch.setattr(server_mod, "WRITE_TIMEOUT", 0.2)
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            # Read the response headers, then stop reading while media flows.
            reader, writer = await asyncio.open_connection("127.0.0.1", server.http_port)
            path = server.flv_url("cam1").split(str(server.http_port), 1)[1]
            writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            async with asyncio.timeout(5):
                while not ch.consumer_count:
                    await asyncio.sleep(0.01)
            big = bytes([0x27, 1, 0, 0, 0]) + b"\x01" * 200_000
            for i in range(60):
                publishers[0].media(9, BASE_TS + 100 * (i + 2), big)
                await asyncio.sleep(0.01)
            async with asyncio.timeout(5):
                while ch.consumer_count:
                    await asyncio.sleep(0.05)
            writer.close()
        finally:
            await server.async_stop()

    async def test_header_change_reaches_an_http_reader(self) -> None:
        server = await _start_server()
        try:
            ch = server.register("cam1", KEY)
            publishers = _wire_start(server, ch)
            changed = AAC_HEADER[:-1] + b"\x10"
            async with (
                aiohttp.ClientSession() as session,
                session.get(server.flv_url("cam1")) as resp,
            ):
                data = b""
                async with asyncio.timeout(5):
                    while len(data) < 13 or len(_parse_flv(data)) < 6:
                        data += await resp.content.read(65536)
                    publishers[0].media(8, BASE_TS + 400, changed)
                    publishers[0].media(9, BASE_TS + 500, INTERFRAME)
                    while not any(body == changed for _, _, body in _parse_flv(data)):
                        data += await resp.content.read(65536)
        finally:
            await server.async_stop()

    async def test_http_start_failure_releases_the_ingest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiohttp import web

        async def boom(self: object) -> None:
            raise OSError("no loopback")

        monkeypatch.setattr(web.TCPSite, "start", boom)
        server = LocalStreamServer(port=0, host="127.0.0.1")
        with pytest.raises(OSError, match="no loopback"):
            await server.async_start()
        port = server.rtmp_port
        probe = socket.socket()
        probe.bind(("127.0.0.1", port))  # the port is free again
        probe.close()


async def test_on_publish_fires_for_every_accepted_publisher() -> None:
    ch = StreamChannel("cam1", KEY)
    calls: list[int] = []
    ch.on_publish = lambda: calls.append(1)
    ch.publish_started(1, None, lambda: None)
    ch.publish_started(2, None, lambda: None)
    assert calls == [1, 1]


async def test_eviction_mid_tls_is_not_a_protocol_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aionanit.local_stream import server as server_mod

    monkeypatch.setattr(server_mod, "MAX_PENDING_PER_PEER", 1)
    server = await _start_server(_self_signed(tmp_path))
    try:
        first_reader, first_writer = await _idle_connect(server.rtmp_port)
        _r, second_writer = await _idle_connect(server.rtmp_port)
        assert await _closed_by_server(first_reader)
        await asyncio.sleep(0.05)
        assert server.protocol_failures == 0
        first_writer.close()
        second_writer.close()
    finally:
        await server.async_stop()


async def test_peer_reset_mid_tls_is_a_protocol_failure(tmp_path: Path) -> None:
    server = await _start_server(_self_signed(tmp_path))
    try:
        reader, writer = await _idle_connect(server.rtmp_port)
        writer.write(b"\x16\x03\x01\x00\x05hello")  # a broken ClientHello
        await writer.drain()
        writer.transport.abort()
        async with asyncio.timeout(15):
            while server.protocol_failures == 0:
                await asyncio.sleep(0.05)
        assert not server._pending
    finally:
        await server.async_stop()


async def test_failing_publish_callback_keeps_the_connection() -> None:
    ch = StreamChannel("cam1", KEY)

    def broken() -> None:
        raise RuntimeError("owner bug")

    ch.on_publish = broken
    ch.publish_started(1, None, lambda: None)
    assert ch.publishing
