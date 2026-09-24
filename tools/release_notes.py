#!/usr/bin/env python3
"""Release notes from the Changelog section of each merged PR.

Every PR describes its user-facing change under a "## Changelog" heading in
its description. Release notes are assembled from those sections, so there is
no changelog file to edit or conflict on, and nothing to commit at release.

Usage:
  release_notes.py <tag>      notes for a release tag (used by release.yaml)
  release_notes.py --preview  notes for what origin/main would ship now
  release_notes.py --check    validate a PR's title and Changelog section
                              (reads PR_TITLE, PR_BODY, PR_LABELS from env)

Notes cover every PR merged since the previous stable release, so a beta
shows everything the next stable will contain.

Standard library only: it runs on a bare CI runner.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

TITLE_RE = re.compile(
    r"^(?P<type>feat|fix|perf|refactor|docs|test|chore|ci|build|revert)"
    r"(?:\([\w./-]+\))?(?P<breaking>!)?: \S"
)
PR_REF_RE = re.compile(r"\(#(\d+)\)$")
SECTION_RE = re.compile(r"^##\s+Changelog\s*$(?P<text>.*?)(?=^#{1,2}\s|\Z)", re.M | re.S | re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
NONE_VALUES = {"", "none", "n/a", "-", "no"}
USER_FACING_TYPES = {"feat", "fix", "perf"}

SECTIONS = {
    "breaking": "Breaking changes",
    "feat": "Added",
    "fix": "Fixed",
    "other": "Changed",
}


@dataclass
class Entry:
    """One PR's contribution to the notes."""

    number: int
    section: str
    text: str
    author: str


def git(*args: str) -> str:
    """Run git and return stdout."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def changelog_section(body: str | None) -> str | None:
    """Return the Changelog section text, or None when absent or opted out."""
    match = SECTION_RE.search(body or "")
    if not match:
        return None
    text = COMMENT_RE.sub("", match.group("text")).strip()
    return None if text.lower() in NONE_VALUES else text


def has_changelog_heading(body: str | None) -> bool:
    """Return True if the body has a Changelog heading at all."""
    return SECTION_RE.search(body or "") is not None


def section_for(title: str) -> str:
    """Map a conventional commit title to a notes section."""
    match = TITLE_RE.match(title)
    if not match:
        return "other"
    if match.group("breaking"):
        return "breaking"
    return match.group("type") if match.group("type") in ("feat", "fix") else "other"


def format_entry(entry: Entry) -> list[str]:
    """Render an entry as bullets, each tagged with its PR number."""
    ref = f" (#{entry.number})"
    lines = entry.text.splitlines()
    if lines and lines[0].lstrip().startswith(("- ", "* ")):
        out: list[str] = []
        for line in lines:
            if line.lstrip().startswith(("- ", "* ")):
                if out:
                    out[-1] += ref
                out.append("- " + line.lstrip()[2:].strip())
            elif line.strip():
                out.append("  " + line.strip())
        out[-1] += ref
        return out
    paragraph = " ".join(line.strip() for line in lines if line.strip())
    return [f"- {paragraph}{ref}"]


def previous_stable(ref: str) -> str | None:
    """Return the newest stable tag strictly before ref, if any."""
    try:
        return git(
            "describe",
            "--tags",
            "--abbrev=0",
            "--match",
            "v[0-9]*.[0-9]*.[0-9]*",
            "--exclude",
            "*-*",
            f"{ref}^",
        )
    except subprocess.CalledProcessError:
        return None


def fetch_entries(since: str | None, ref: str) -> list[Entry]:
    """Collect entries from PRs squash-merged in (since, ref]."""
    rev_range = f"{since}..{ref}" if since else ref
    entries: list[Entry] = []
    for subject in git("log", "--format=%s", rev_range).splitlines():
        match = PR_REF_RE.search(subject)
        if not match:
            continue
        number = int(match.group(1))
        pr = json.loads(
            subprocess.run(
                ["gh", "pr", "view", str(number), "--json", "title,body,author"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        text = changelog_section(pr["body"])
        if text:
            entries.append(Entry(number, section_for(pr["title"]), text, pr["author"]["login"]))
    return entries


def render(entries: list[Entry], repo: str, since: str | None, ref: str) -> str:
    """Render the full notes document."""
    parts: list[str] = []
    for key, heading in SECTIONS.items():
        bullets = [
            line
            for entry in sorted(entries, key=lambda e: e.number)
            if entry.section == key
            for line in format_entry(entry)
        ]
        if bullets:
            parts.append(f"## {heading}\n\n" + "\n".join(bullets))
    if not parts:
        parts.append("No user-facing changes.")
    authors = sorted({e.author for e in entries if not e.author.startswith("app/")})
    if authors:
        parts.append("Thanks to " + ", ".join(f"@{a}" for a in authors) + ".")
    if since:
        parts.append(f"**Full changelog**: https://github.com/{repo}/compare/{since}...{ref}")
    return "\n\n".join(parts) + "\n"


def check_pr() -> int:
    """Validate the PR in the environment; print problems, return exit code."""
    title = os.environ.get("PR_TITLE", "")
    body = os.environ.get("PR_BODY", "")
    labels = json.loads(os.environ.get("PR_LABELS", "[]"))
    release_label = next((lbl for lbl in labels if lbl.startswith("release:")), None)
    errors: list[str] = []

    match = TITLE_RE.match(title)
    if not match:
        errors.append(
            f"PR title {title!r} is not a conventional commit, e.g. 'fix: handle token "
            "refresh during reconnect'. It becomes the squash commit on main."
        )
    elif release_label and not changelog_section(body):
        errors.append(
            f"The {release_label} label publishes a release, so the description needs a "
            "'## Changelog' section describing the change for users."
        )
    elif match.group("type") in USER_FACING_TYPES and not has_changelog_heading(body):
        errors.append(
            "feat/fix/perf PRs need a '## Changelog' section in the description: one or "
            "more user-facing sentences, or 'none' if users won't notice the change."
        )

    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print("PR title and changelog look good.")
    return 1 if errors else 0


def main(argv: list[str]) -> int:
    """Entry point."""
    if argv == ["--check"]:
        return check_pr()
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    repo = (
        os.environ.get("GITHUB_REPOSITORY")
        or subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    ref = "origin/main" if argv[0] == "--preview" else argv[0]
    since = previous_stable(ref)
    shown_ref = "main" if argv[0] == "--preview" else ref
    print(render(fetch_entries(since, ref), repo, since, shown_ref), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
