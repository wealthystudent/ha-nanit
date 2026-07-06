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


def test_card_source_contains_one_shot_integration_reload_fallback() -> None:
    """A persistent black/unavailable stream should trigger one integration reload fallback."""
    card = _read(SRC_DIR / "nanit-card.ts")

    assert "STREAM_UNAVAILABLE_RELOAD_TICKS" in card
    assert "_reloadIntegrationOnce" in card
    assert 'callService("homeassistant", "reload_config_entry"' in card
    assert "entity_id: entities.camera" in card
    assert "this._integrationReloadAttempted" in card


def test_bundled_card_contains_one_shot_integration_reload_fallback() -> None:
    """The shipped bundle should include the one-shot reload fallback after frontend build."""
    bundle = _read(BUNDLE)

    assert "reload_config_entry" in bundle
    assert "homeassistant" in bundle


def test_bundled_card_contains_stream_liveness_watchdog() -> None:
    """The shipped bundle should include stream remount markers after frontend build."""
    bundle = _read(BUNDLE)

    assert "data-stream-epoch" in bundle
    assert "ha-hls-player, ha-web-rtc-player" in bundle
