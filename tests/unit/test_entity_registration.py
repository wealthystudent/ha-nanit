"""Entity registration snapshot tests.

Verifies the exact set of entities each platform registers for different
camera configurations. This prevents accidental entity addition/removal
regressions (like the v1.4.0-beta.3 night light entity removal).

Uses pytest-syrupy for snapshot assertions. To update snapshots after an
intentional change, run::

    pytest tests/unit/test_entity_registration.py --snapshot-update

See https://github.com/syrupy-project/syrupy for details.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from syrupy.assertion import SnapshotAssertion

_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

_MODELS = importlib.import_module("aionanit.models")
Baby = _MODELS.Baby
CameraState = _MODELS.CameraState
ConnectionInfo = _MODELS.ConnectionInfo
ConnectionState = _MODELS.ConnectionState
ControlState = _MODELS.ControlState
SensorState = _MODELS.SensorState
SettingsState = _MODELS.SettingsState

from custom_components.nanit import (
    binary_sensor as binary_sensor_platform,
)
from custom_components.nanit import (
    camera as camera_platform,
)
from custom_components.nanit import (
    light as light_platform,
)
from custom_components.nanit import (
    number as number_platform,
)
from custom_components.nanit import (
    select as select_platform,
)
from custom_components.nanit import (
    sensor as sensor_platform,
)
from custom_components.nanit import (
    switch as switch_platform,
)
from custom_components.nanit.aionanit_sl.models import SoundLightFullState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_entities(entities: list[Any]) -> list[dict[str, Any]]:
    """Extract stable, snapshot-worthy attributes from a list of HA entities.

    Returns a sorted list of dicts so that snapshot diffs are deterministic
    and easy to review.
    """
    result: list[dict[str, Any]] = []
    for entity in entities:
        info: dict[str, Any] = {
            "class": type(entity).__name__,
            "unique_id": entity.unique_id,
        }

        if hasattr(entity, "_attr_translation_key"):
            info["translation_key"] = entity._attr_translation_key
        elif hasattr(entity, "entity_description"):
            info["translation_key"] = getattr(entity.entity_description, "translation_key", None)

        if hasattr(entity, "entity_description"):
            info["device_class"] = getattr(entity.entity_description, "device_class", None)
            info["entity_category"] = getattr(entity.entity_description, "entity_category", None)
        else:
            info["device_class"] = getattr(entity, "_attr_device_class", None)
            info["entity_category"] = getattr(entity, "_attr_entity_category", None)

        features = getattr(entity, "_attr_supported_features", None)
        if features is not None:
            info["supported_features"] = int(features)

        color_modes = getattr(entity, "_attr_supported_color_modes", None)
        if color_modes is not None:
            info["color_modes"] = sorted(str(m) for m in color_modes)

        device_info = entity.device_info
        if device_info:
            identifiers = device_info.get("identifiers", set())
            info["device_identifiers"] = sorted(tuple(i) for i in identifiers)

        result.append(info)

    return sorted(result, key=lambda e: e["unique_id"])


# ---------------------------------------------------------------------------
# Mock coordinator factories
# ---------------------------------------------------------------------------


def _camera_state() -> CameraState:
    return CameraState(
        sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
        settings=SettingsState(volume=50, sleep_mode=False, night_vision=True),
        control=ControlState(),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )


def _push_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = _camera_state()
    coordinator.camera = MagicMock(uid="cam_1", baby_uid="baby_1")
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    coordinator.last_update_success = True
    coordinator.connected = True
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _cloud_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = []
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _sl_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = SoundLightFullState()
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    coordinator.last_update_success = True
    coordinator.connected = True
    coordinator.sound_light = MagicMock()
    coordinator.sound_light.async_set_light_enabled = AsyncMock()
    coordinator.sound_light.async_set_brightness = AsyncMock()
    coordinator.sound_light.async_set_color = AsyncMock()
    coordinator.sound_light.async_set_power = AsyncMock()
    coordinator.sound_light.async_set_sound_on = AsyncMock()
    coordinator.sound_light.async_set_track = AsyncMock()
    coordinator.sound_light.async_set_volume = AsyncMock()
    coordinator.sound_light.connection_mode = "local"
    return coordinator


def _network_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = MagicMock()
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    coordinator.last_update_success = True
    return coordinator


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------


def _baseline_cam_data() -> MagicMock:
    """Camera with push coordinator only (no S&L, no cloud, no network)."""
    push = _push_coordinator()
    cam_data = MagicMock()
    cam_data.push_coordinator = push
    cam_data.camera = push.camera
    cam_data.cloud_coordinator = None
    cam_data.sound_light_coordinator = None
    cam_data.network_coordinator = None
    return cam_data


def _full_cam_data() -> MagicMock:
    """Camera with all optional coordinators enabled."""
    push = _push_coordinator()
    cam_data = MagicMock()
    cam_data.push_coordinator = push
    cam_data.camera = push.camera
    cam_data.cloud_coordinator = _cloud_coordinator()
    cam_data.sound_light_coordinator = _sl_coordinator()
    cam_data.network_coordinator = _network_coordinator()
    return cam_data


def _mock_entry(cam_data: MagicMock) -> MagicMock:
    return MagicMock(runtime_data=MagicMock(cameras={"cam_1": cam_data}))


# ---------------------------------------------------------------------------
# Platform setup + capture helper
# ---------------------------------------------------------------------------


async def _setup_and_capture(
    platform_module: Any,
    cam_data: MagicMock,
) -> list[Any]:
    """Call a platform's async_setup_entry and return the captured entities."""
    entry = _mock_entry(cam_data)
    async_add_entities = MagicMock()
    await platform_module.async_setup_entry(MagicMock(), entry, async_add_entities)
    assert async_add_entities.call_count == 1, "async_add_entities should be called exactly once"
    return list(async_add_entities.call_args.args[0])


# ---------------------------------------------------------------------------
# Snapshot tests -- 7 platforms x 2 scenarios = 14 test cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario_name", "cam_data_factory"),
    [
        ("baseline", _baseline_cam_data),
        ("full", _full_cam_data),
    ],
    ids=["baseline", "full"],
)
class TestEntityRegistration:
    async def test_camera(
        self,
        scenario_name: str,
        cam_data_factory: Any,
        snapshot: SnapshotAssertion,
    ) -> None:
        entities = await _setup_and_capture(camera_platform, cam_data_factory())
        assert _serialize_entities(entities) == snapshot

    async def test_sensor(
        self,
        scenario_name: str,
        cam_data_factory: Any,
        snapshot: SnapshotAssertion,
    ) -> None:
        entities = await _setup_and_capture(sensor_platform, cam_data_factory())
        assert _serialize_entities(entities) == snapshot

    async def test_binary_sensor(
        self,
        scenario_name: str,
        cam_data_factory: Any,
        snapshot: SnapshotAssertion,
    ) -> None:
        entities = await _setup_and_capture(binary_sensor_platform, cam_data_factory())
        assert _serialize_entities(entities) == snapshot

    async def test_switch(
        self,
        scenario_name: str,
        cam_data_factory: Any,
        snapshot: SnapshotAssertion,
    ) -> None:
        entities = await _setup_and_capture(switch_platform, cam_data_factory())
        assert _serialize_entities(entities) == snapshot

    async def test_number(
        self,
        scenario_name: str,
        cam_data_factory: Any,
        snapshot: SnapshotAssertion,
    ) -> None:
        entities = await _setup_and_capture(number_platform, cam_data_factory())
        assert _serialize_entities(entities) == snapshot

    async def test_light(
        self,
        scenario_name: str,
        cam_data_factory: Any,
        snapshot: SnapshotAssertion,
    ) -> None:
        entities = await _setup_and_capture(light_platform, cam_data_factory())
        assert _serialize_entities(entities) == snapshot

    async def test_select(
        self,
        scenario_name: str,
        cam_data_factory: Any,
        snapshot: SnapshotAssertion,
    ) -> None:
        entities = await _setup_and_capture(select_platform, cam_data_factory())
        assert _serialize_entities(entities) == snapshot
