"""Tests for sanitize_name utility."""

from __future__ import annotations

import pytest

from custom_components.nanit.sanitize import display_name, sanitize_name

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]


def test_plain_name_unchanged() -> None:
    assert sanitize_name("Nursery") == "Nursery"


def test_strips_script_tag() -> None:
    assert sanitize_name('<script>alert("xss")</script>') == 'alert("xss")'


def test_strips_img_onerror() -> None:
    assert sanitize_name("<img src=x onerror=alert(1)>") == ""


def test_strips_nested_tags() -> None:
    assert sanitize_name("<b><i>Baby</i></b> Room") == "Baby Room"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mom & Dad", "Mom & Dad"),
        ("Zoë O'Brien", "Zoë O'Brien"),
        ('The "Big" Room', 'The "Big" Room'),
        ("A & B < C", "A & B < C"),
    ],
)
def test_plain_text_is_not_escaped(raw: str, expected: str) -> None:
    assert sanitize_name(raw) == expected


def test_pre_encoded_entities_are_decoded() -> None:
    assert sanitize_name("A &amp; B") == "A & B"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("&lt;script&gt;alert(1)&lt;/script&gt;Nursery", "alert(1)Nursery"),
        ("&amp;lt;b&amp;gt;Nursery&amp;lt;/b&amp;gt;", "Nursery"),
        ("&#60;img src=x onerror=alert(1)&#62;Nursery", "Nursery"),
    ],
)
def test_encoded_tags_are_stripped(raw: str, expected: str) -> None:
    """Entities are decoded before the tag strip, so encoded tags never survive."""
    assert sanitize_name(raw) == expected


def test_unclosed_tag_stays_literal_text() -> None:
    """A "<" without a closing ">" is no tag, so it's kept as text."""
    assert sanitize_name("<img src=x onerror=alert(1)") == "<img src=x onerror=alert(1)"


def test_collapses_whitespace() -> None:
    assert sanitize_name("  Baby   Room  ") == "Baby Room"


def test_empty_string() -> None:
    assert sanitize_name("") == ""


def test_unicode_preserved() -> None:
    assert sanitize_name("Bébé's Room 🍼") == "Bébé's Room 🍼"


def test_none_returns_empty() -> None:
    assert sanitize_name(None) == ""


def test_display_name_with_valid_name() -> None:
    assert display_name("Nursery", "abc12345") == "Nursery"


def test_display_name_with_none_falls_back_to_uid() -> None:
    assert display_name(None, "abc12345-6789") == "Baby abc12345"


def test_display_name_with_empty_falls_back_to_uid() -> None:
    assert display_name("", "abc12345-6789") == "Baby abc12345"


def test_display_name_with_html_only_falls_back_to_uid() -> None:
    assert display_name("<script></script>", "abc12345") == "Baby abc12345"
