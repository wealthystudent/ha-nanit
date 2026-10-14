"""Tests for the local RTMP(S) receiver and its HTTP-FLV fan-out."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import random
import ssl
import struct
import time
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
