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


def test_card_remounts_stalled_stream_after_initial_load_timeout() -> None:
    """The card should recover when HA's camera stream element stalls on refresh."""
    card = _read(SRC_DIR / "nanit-card.ts")

    assert "STREAM_STALL_CHECKS" in card
    assert "_retryStreamLoad()" in card
    assert "this._streamEpoch += 1" in card
    assert "keyed(`${entities.camera}-${this._streamEpoch}`" in card


def test_bundled_card_contains_semantic_override_support() -> None:
    """The shipped bundle should include the override logic after frontend build."""
    bundle = _read(BUNDLE)

    assert "temperature_entity_id" in bundle
    assert "humidity_entity_id" in bundle


def test_bundled_card_contains_stalled_stream_remount_support() -> None:
    """The shipped bundle should include stalled-stream remount recovery."""
    bundle = _read(BUNDLE)

    assert "data-stream-epoch" in bundle
    assert "_streamEpoch" in bundle
