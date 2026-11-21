#!/usr/bin/env python3
"""PreToolUse hook: stop git commands that break the repo's rules.

Permission rules can't express "force-push, but only with a lease", and the
docs tell contributors to re-sign their own branch with --force-with-lease.
This hook allows that and blocks a bare --force, any push to main, skipped
hooks and changes to commit signing. Like the permission rules it reads
command text, so it is an accident guard, not a security boundary.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys

SIGNING_KEYS = re.compile(r"^(commit|tag|rebase)\.gpgsign$|^gpg\.|^user\.signingkey$", re.I)


def segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands, as word lists."""
    parts = re.split(r"&&|\|\||[;|\n]", command)
    words = []
    for part in parts:
        try:
            split = shlex.split(part)
        except ValueError:
            split = part.split()
        while split and re.match(r"^\w+=", split[0]):  # FOO=bar git ...
            split = split[1:]
        if split:
            words.append(split)
    return words


def current_branch(cwd: str | None) -> str:
    """Return the checked-out branch in cwd, or '' if git can't tell."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def problem(words: list[str], cwd: str | None = None) -> str | None:
    """Why this git command is not allowed, or None."""
    if not words or words[0] != "git":
        return None
    args = words[1:]
    configs = []
    while args and args[0].startswith("-"):  # git -C dir -c key=value <subcommand>
        option = args.pop(0)
        if option in ("-C", "-c") and args:
            value = args.pop(0)
            if option == "-c":
                configs.append(value)
    for value in configs:
        key = value.split("=", 1)[0]
        if SIGNING_KEYS.search(key):
            return f"`-c {value}` changes commit signing. Commits must stay signed."
    if not args:
        return None
    sub, rest = args[0], args[1:]

    if sub == "config":
        words_ = [a for a in rest if not a.startswith("-")]
        action = words_.pop(0) if words_ and words_[0] in ("set", "unset", "get", "list") else ""
        writes = (
            action in ("set", "unset")
            or (not action and len(words_) > 1)
            or any(a.startswith(("--unset", "--replace-all", "--add", "--rename-section",
                                 "--remove-section", "--edit")) for a in rest)
        )  # fmt: skip
        if writes and (not words_ or SIGNING_KEYS.search(words_[0])):
            return "Changing signing config is not allowed. Use the host's signing as it is."
    if "--no-verify" in rest or (sub == "commit" and "-n" in rest):
        return "Skipping git hooks is not allowed; fix what the hook reports."
    if "--no-gpg-sign" in rest:
        return "Commits must be signed; drop --no-gpg-sign."
    if sub == "push":
        bare = "--force" in rest or any(re.match(r"^-[a-zA-Z]*f", a) for a in rest)
        plus = any(a.startswith("+") for a in rest if not a.startswith("-"))
        if bare or plus:
            return "Use --force-with-lease, and only on a branch you created."
        refs = [a for a in rest if not a.startswith("-")][1:]
        to_main = any(r.split(":")[-1] in ("main", "refs/heads/main") for r in refs)
        implicit = not refs or refs == ["HEAD"]  # pushes the current branch
        if to_main or (implicit and current_branch(cwd) == "main"):
            return "Never push to main; open a PR."
    return None


def main() -> int:
    """Exit 2 with the reason on stderr to block the command."""
    data = json.load(sys.stdin)
    command = data.get("tool_input", {}).get("command", "")
    for words in segments(command):
        if reason := problem(words, data.get("cwd")):
            sys.stderr.write(f"Blocked by .claude/hooks/guard-git.py: {reason}\n")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
