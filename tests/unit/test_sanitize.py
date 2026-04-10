"""Tests for sanitize_name utility."""

from __future__ import annotations

import pytest

from custom_components.nanit.sanitize import sanitize_name

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]


def test_plain_name_unchanged() -> None:
    assert sanitize_name("Nursery") == "Nursery"


def test_strips_script_tag() -> None:
    assert sanitize_name('<script>alert("xss")</script>') == "alert(&quot;xss&quot;)"


def test_strips_img_onerror() -> None:
    assert sanitize_name("<img src=x onerror=alert(1)>") == ""


def test_strips_nested_tags() -> None:
    assert sanitize_name("<b><i>Baby</i></b> Room") == "Baby Room"


def test_escapes_html_entities() -> None:
    assert sanitize_name("A & B < C") == "A &amp; B &lt; C"


def test_pre_encoded_entities_round_trip() -> None:
    assert sanitize_name("A &amp; B") == "A &amp; B"


def test_collapses_whitespace() -> None:
    assert sanitize_name("  Baby   Room  ") == "Baby Room"


def test_empty_string() -> None:
    assert sanitize_name("") == ""


def test_unicode_preserved() -> None:
    assert sanitize_name("Bébé's Room 🍼") == "Bébé&#x27;s Room 🍼"
