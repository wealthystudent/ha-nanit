"""Tests for Nanit card semantic sensor override support."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "frontend" / "src"
BUNDLE = ROOT / "custom_components" / "nanit" / "frontend" / "nanit-card.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_card_config_exposes_temperature_and_humidity_override_fields() -> None:
    """The card config contract should expose semantic sensor entity overrides."""
    types = _read(SRC_DIR / "types.ts")

    assert "temperature_entity_id?: string;" in types
    assert "humidity_entity_id?: string;" in types


def test_sensor_overlay_prefers_semantic_override_entities() -> None:
    """The overlay renderer should prefer semantic config overrides over discovered entities."""
    card = _read(SRC_DIR / "nanit-card.ts")

    assert "this._config.temperature_entity_id || entities.temperature" in card
    assert "this._config.humidity_entity_id || entities.humidity" in card
    assert "_renderSensorOverlays(entities" in card


def test_visual_editor_includes_semantic_override_entity_pickers() -> None:
    """The visual editor should expose optional semantic override pickers."""
    editor = _read(SRC_DIR / "nanit-card-editor.ts")

    assert '"temperature_entity_id"' in editor
    assert '"humidity_entity_id"' in editor
    assert "Temperature Entity Override" in editor
    assert "Humidity Entity Override" in editor


def test_bundled_card_contains_semantic_override_support() -> None:
    """The shipped bundle should include the override logic after frontend build."""
    bundle = _read(BUNDLE)

    assert "temperature_entity_id" in bundle
    assert "humidity_entity_id" in bundle


def test_card_source_contains_stream_liveness_watchdog() -> None:
    """The card should continuously monitor live video progress and remount frozen streams."""
    card = _read(SRC_DIR / "nanit-card.ts")

    assert "STREAM_STALL_TICKS" in card
    assert "_findStreamVideo" in card
    assert "ha-hls-player, ha-web-rtc-player" in card
    assert "video.currentTime > this._lastVideoTime" in card
    assert "!this._sawProgress || video.paused" in card
    assert "_reloadStream" in card
    assert "data-stream-epoch" in card


def test_card_source_requests_backend_stream_reset_before_recovery_remount() -> None:
    """Frontend recovery should clear HA's cached backend stream before remounting."""
    card = _read(SRC_DIR / "nanit-card.ts")

    assert "STREAM_STARTUP_RELOAD_TICKS" in card
    assert "_recoverStream" in card
    assert 'callService("nanit", "reset_stream"' in card
    assert "async _recoverStream" in card
    assert "await this._requestBackendStreamReset()" in card
    assert "this._reloadStream()" in card
    assert "reload_config_entry" not in card


def test_bundled_card_requests_backend_stream_reset_before_recovery_remount() -> None:
    """The shipped bundle should include the lighter Nanit stream reset service."""
    bundle = _read(BUNDLE)

    assert "reset_stream" in bundle
    assert "reload_config_entry" not in bundle


def test_card_source_recovers_stream_on_page_resume() -> None:
    """Mobile/browser resume should force a viewer-scoped stream recovery."""
    card = _read(SRC_DIR / "nanit-card.ts")

    assert "visibilitychange" in card
    assert "pageshow" in card
    assert "focus" in card
    assert "_recoverStreamOnResume" in card


def test_card_styles_force_stable_stream_aspect_ratio() -> None:
    """The stream area should reserve 16:9 space even before video internals load."""
    styles = _read(SRC_DIR / "styles.ts")

    assert ".stream-wrap" in styles
    assert "aspect-ratio: 16 / 9" in styles
    assert ".stream-click ha-camera-stream" in styles


def test_card_source_resets_stream_bookkeeping_whenever_camera_turns_off() -> None:
    """Power-off cleanup must not depend on the stream having reached loaded state.

    The reset fires on the on→off transition in willUpdate (not render —
    mutating reactive state during render is a Lit anti-pattern).
    """
    card = _read(SRC_DIR / "nanit-card.ts")

    assert "if (!cameraOn && this._wasCameraOn)" in card
    assert "_resetStreamState" in card
    assert "this._streamMountedAt = 0" in card
    assert "this._watchedEpoch = -1" in card


def test_bundled_card_resets_stream_bookkeeping_whenever_camera_turns_off() -> None:
    """The shipped bundle should contain unconditional power-off stream cleanup."""
    bundle = _read(BUNDLE)

    assert "_resetStreamState" in bundle
    assert "_watchedEpoch=-1" in bundle


def test_bundled_card_contains_stream_liveness_watchdog() -> None:
    """The shipped bundle should include stream remount markers after frontend build."""
    bundle = _read(BUNDLE)

    assert "data-stream-epoch" in bundle
    assert "ha-hls-player, ha-web-rtc-player" in bundle
