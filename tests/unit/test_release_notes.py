"""Tests for tools/release_notes.py (release notes from PR Changelog sections)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


rn = _load()

TEMPLATE_BODY = """## What does this do

Fixes the thing.

## Changelog

<!-- One or more user-facing sentences, or "none". -->
Live video now starts on installs without `default_config:`.

## How I tested this

Dev instance.
"""


def test_changelog_section_extracts_text_between_headings() -> None:
    assert (
        rn.changelog_section(TEMPLATE_BODY)
        == "Live video now starts on installs without `default_config:`."
    )


@pytest.mark.parametrize("value", ["none", "None", "n/a", "-", "", "<!-- only a comment -->"])
def test_changelog_section_opt_out(value: str) -> None:
    assert rn.changelog_section(f"## Changelog\n\n{value}\n") is None
    assert rn.has_changelog_heading(f"## Changelog\n\n{value}\n")


def test_changelog_section_missing() -> None:
    assert rn.changelog_section("Just a description.") is None
    assert rn.changelog_section(None) is None
    assert not rn.has_changelog_heading("Just a description.")


def test_changelog_section_keeps_subheadings() -> None:
    body = "## Changelog\n\n### Detail\nText\n## Next\nignored"
    assert rn.changelog_section(body) == "### Detail\nText"


@pytest.mark.parametrize(
    ("title", "section"),
    [
        ("feat: add night vision toggle", "feat"),
        ("fix(sl): keep light color", "fix"),
        ("feat!: drop legacy entities", "breaking"),
        ("chore: bump deps", "other"),
        ("not conventional", "other"),
    ],
)
def test_section_for(title: str, section: str) -> None:
    assert rn.section_for(title) == section


def test_format_entry_paragraph_becomes_one_bullet() -> None:
    entry = rn.Entry(12, "fix", "First line\ncontinues here.", "someone")
    assert rn.format_entry(entry) == ["- First line continues here. (#12)"]


def test_format_entry_bullets_each_get_ref() -> None:
    entry = rn.Entry(7, "feat", "- One\n  wrapped\n* Two", "someone")
    assert rn.format_entry(entry) == ["- One", "  wrapped (#7)", "- Two (#7)"]


def test_render_groups_sections_and_credits_humans() -> None:
    entries = [
        rn.Entry(3, "fix", "Fixed B.", "alice"),
        rn.Entry(1, "feat", "Added A.", "bob"),
        rn.Entry(2, "breaking", "Removed C.", "alice"),
        rn.Entry(4, "other", "Changed D.", "app/github-actions"),
    ]
    notes = rn.render(entries, "o/r", "v1.0.0", "v1.1.0")
    assert notes.index("## Breaking changes") < notes.index("## Added") < notes.index("## Fixed")
    assert "- Added A. (#1)" in notes
    assert "Thanks to @alice, @bob." in notes
    assert notes.rstrip().endswith("https://github.com/o/r/compare/v1.0.0...v1.1.0")


def test_render_empty() -> None:
    assert rn.render([], "o/r", None, "v0.1.0") == "No user-facing changes.\n"


def _check(
    monkeypatch: pytest.MonkeyPatch, title: str, body: str, labels: list[str] | None = None
) -> int:
    monkeypatch.setenv("PR_TITLE", title)
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.setenv("PR_LABELS", json.dumps(labels or []))
    return int(rn.check_pr())


def test_check_rejects_non_conventional_title(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _check(monkeypatch, "Update stuff", TEMPLATE_BODY) == 1


def test_check_fix_needs_heading_but_allows_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _check(monkeypatch, "fix: x", "no section") == 1
    assert _check(monkeypatch, "fix: x", "## Changelog\n\nnone") == 0
    assert _check(monkeypatch, "fix: x", TEMPLATE_BODY) == 0


def test_check_release_label_needs_real_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "## Changelog\n\nnone"
    assert _check(monkeypatch, "chore: x", body, ["release:patch"]) == 1
    assert _check(monkeypatch, "chore: x", TEMPLATE_BODY, ["release:patch"]) == 0


def test_check_chore_without_section_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _check(monkeypatch, "chore(deps): bump ruff", "") == 0
