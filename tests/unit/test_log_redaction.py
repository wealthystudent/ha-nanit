"""Tests for masking the access token in Home Assistant's stream logs."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import av
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pytest_homeassistant_custom_component.common import MockConfigEntry

# camera.py imports turbojpeg, which the test environment does not install.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from custom_components.nanit.camera import NanitCameraEntity
from custom_components.nanit.const import DOMAIN
from custom_components.nanit.log_redaction import (
    STREAM_LOGGERS,
    StreamTokenFilter,
    async_attach_to_camera_stream,
    async_install_stream_token_filter,
    async_remove_stream_token_filter,
    redact_stream_token,
)

from .conftest import MOCK_EMAIL, mock_entry_data_v2
from .test_entities import _camera_state, _push_coordinator

# A made-up token with a JWT's shape (base64url segments joined by dots), like Nanit's.
TOKEN = "fakeHeaderAAAA1111.fakePayloadBBBB2222.fakeSignature-_CCCC3333"
URL = f"rtmps://media-secured.nanit.com/nanit/baby123.{TOKEN}"
MASKED = "rtmps://media-secured.nanit.com/nanit/baby123.***"


# Spelled out here rather than imported, so dropping a logger from the module's
# list fails a test instead of quietly dropping that logger's test case too.
EXPECTED_LOGGERS = (
    "homeassistant.components.stream",
    "homeassistant.components.go2rtc.server",
)
CAMERA = "camera.nursery"
CAMERA_STREAM_LOGGER = f"homeassistant.components.stream.stream.{CAMERA}"


def _filters_on(name: str) -> list[StreamTokenFilter]:
    return [f for f in logging.getLogger(name).filters if isinstance(f, StreamTokenFilter)]


@pytest.fixture(autouse=True)
def _clean_loggers():
    """Loggers outlive each test's hass, so start and end every test without the filter."""

    def strip() -> None:
        for name in (*EXPECTED_LOGGERS, CAMERA_STREAM_LOGGER):
            for f in _filters_on(name):
                logging.getLogger(name).removeFilter(f)

    strip()
    yield
    strip()


def test_every_global_logger_is_covered() -> None:
    assert set(STREAM_LOGGERS) == set(EXPECTED_LOGGERS)


def test_go2rtc_still_relays_through_its_module_logger() -> None:
    """A rename in Home Assistant would silently stop the go2rtc redaction.

    The source is read rather than imported because the go2rtc module needs
    go2rtc_client, which the test environment does not install. The relay logs
    through the module-level logger, whose name is the module path.
    """
    components = Path(importlib.import_module("homeassistant.components").__path__[0])
    source = (components / "go2rtc" / "server.py").read_text()

    assert "_LOGGER = logging.getLogger(__name__)" in source
    assert "_LOGGER.log(loglevel, msg)" in source


@pytest.fixture
def token_filter(hass: HomeAssistant):
    async_install_stream_token_filter(hass, "entry")
    yield
    async_remove_stream_token_filter(hass, "entry")


@pytest.mark.parametrize(
    "message",
    [
        # stream/__init__.py, from the worker's redact_av_error_string()
        f"Error from stream worker: Error opening stream (HTTP_UNAUTHORIZED, Server returned "
        f"401 Unauthorized (authorization failed)) {URL}",
        f"Started stream: {URL}",
        f"Restarting stream worker in 10 seconds: {URL}",
        # go2rtc's own warning, as Home Assistant relays it
        f'12:00:01.000 WRN [streams] error="read: connection reset by peer" url={URL} '
        f"caller=internal/streams/producer.go:170",
    ],
)
def test_redact_masks_only_the_token(message: str) -> None:
    redacted = redact_stream_token(message)

    assert TOKEN not in redacted
    assert MASKED in redacted
    assert redacted == message.replace(URL, MASKED)


@pytest.mark.parametrize(
    ("url", "masked"),
    [
        (
            f"rtmps://MEDIA-SECURED.NANIT.COM/nanit/baby123.{TOKEN}",
            "rtmps://MEDIA-SECURED.NANIT.COM/nanit/baby123.***",
        ),
        (
            f"rtmps://media-secured.nanit.com:443/nanit/baby123.{TOKEN}",
            "rtmps://media-secured.nanit.com:443/nanit/baby123.***",
        ),
        (
            f"rtmps%3A%2F%2Fmedia-secured.nanit.com%2Fnanit%2Fbaby123.{TOKEN}",
            "rtmps%3A%2F%2Fmedia-secured.nanit.com%2Fnanit%2Fbaby123.***",
        ),
    ],
)
def test_redact_handles_other_url_spellings(url: str, masked: str) -> None:
    assert redact_stream_token(f"Error opening input: {url}") == f"Error opening input: {masked}"


def test_redact_leaves_other_text_alone() -> None:
    message = "Error from stream worker: rtsp://192.168.1.10/live timed out"
    assert redact_stream_token(message) == message


@pytest.mark.parametrize("logger_name", EXPECTED_LOGGERS)
def test_filter_redacts_records_on_each_stream_logger(
    hass: HomeAssistant,
    token_filter: None,
    caplog: pytest.LogCaptureFixture,
    logger_name: str,
) -> None:
    caplog.set_level(logging.DEBUG, logger=logger_name)

    logging.getLogger(logger_name).warning("Error from stream worker: %s", URL)

    assert TOKEN not in caplog.text
    assert MASKED in caplog.text


def test_filter_redacts_the_traceback(
    hass: HomeAssistant, token_filter: None, caplog: pytest.LogCaptureFixture
) -> None:
    try:
        raise OSError(f"Error opening input: {URL}")
    except OSError:
        logging.getLogger("homeassistant.components.stream").exception("Stream failed")

    assert TOKEN not in caplog.text
    assert MASKED in caplog.text
    assert "OSError" in caplog.text


def test_filter_leaves_unrelated_records_untouched(
    hass: HomeAssistant, token_filter: None, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("homeassistant.components.stream")
    with caplog.at_level(logging.DEBUG, logger="homeassistant.components.stream"):
        logger.debug("Stream %s idle for %d seconds", "camera.front", 30)

    record = caplog.records[-1]
    assert record.msg == "Stream %s idle for %d seconds"
    assert record.args == ("camera.front", 30)


def test_filter_is_shared_and_removed_with_the_last_entry(hass: HomeAssistant) -> None:
    async_install_stream_token_filter(hass, "one")
    async_install_stream_token_filter(hass, "two")
    for name in EXPECTED_LOGGERS:
        assert len(_filters_on(name)) == 1

    async_remove_stream_token_filter(hass, "one")
    for name in EXPECTED_LOGGERS:
        assert len(_filters_on(name)) == 1

    async_remove_stream_token_filter(hass, "two")
    for name in EXPECTED_LOGGERS:
        assert _filters_on(name) == []


async def test_setup_installs_and_unload_removes_the_filter(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for name in EXPECTED_LOGGERS:
        assert len(_filters_on(name)) == 1

    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        assert await hass.config_entries.async_unload(entry.entry_id)

    for name in EXPECTED_LOGGERS:
        assert _filters_on(name) == []


async def _fail_a_real_stream(hass: HomeAssistant) -> None:
    """Run Home Assistant's own stream worker into a 401 on a Nanit URL."""
    from homeassistant.components.camera import DynamicStreamSettings
    from homeassistant.components.stream import create_stream
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "stream", {})
    with (
        patch("homeassistant.components.stream.KeyFrameConverter"),
        patch("homeassistant.components.stream._should_retry", return_value=False),
        patch("av.open", side_effect=av.FFmpegError(1, "Server returned 401 Unauthorized", URL)),
    ):
        stream = create_stream(hass, URL, {}, DynamicStreamSettings(), stream_label=CAMERA)
        await stream.start()
        assert stream._thread is not None
        await hass.async_add_executor_job(stream._thread.join, 10)


async def test_real_stream_error_leaks_the_token_without_the_filter(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Control: proves the next test drives the path that actually leaks."""
    await _fail_a_real_stream(hass)

    assert "Error from stream worker" in caplog.text
    assert TOKEN in caplog.text


async def test_real_stream_error_is_masked_on_the_camera_stream_logger(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    async_attach_to_camera_stream(CAMERA)

    await _fail_a_real_stream(hass)

    assert "Error from stream worker" in caplog.text
    assert TOKEN not in caplog.text
    assert MASKED in caplog.text


async def test_camera_entity_filters_its_stream_logger_while_added(hass: HomeAssistant) -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    entity = NanitCameraEntity(coordinator, MagicMock(uid="cam_1"))
    entity.hass = hass
    entity.entity_id = CAMERA

    with patch.object(CoordinatorEntity, "async_added_to_hass", AsyncMock()):
        await entity.async_added_to_hass()
    assert len(_filters_on(CAMERA_STREAM_LOGGER)) == 1

    with patch.object(CoordinatorEntity, "async_will_remove_from_hass", AsyncMock()):
        await entity.async_will_remove_from_hass()
    assert _filters_on(CAMERA_STREAM_LOGGER) == []
