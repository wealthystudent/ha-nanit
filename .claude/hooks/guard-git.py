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
import os
import re
import shlex
import subprocess
import sys

CONFIG_OPTIONS_WITH_VALUE = {"-f", "--file", "--blob", "--type", "--default", "--comment"}
SIGNING_KEYS = re.compile(r"^(commit|tag|rebase)\.gpgsign$|^gpg\.|^user\.signingkey$", re.I)


# Commands that run the rest of their words as a command, and the options of
# theirs that take a value.
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "nohup": set(),
    "command": set(),
    "exec": set(),
    "time": set(),
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-U"},
}
SHELLS = {"sh", "bash", "zsh", "dash"}
SHELL_C_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")  # -c, and clusters like -lc or -xc


def unwrap(words: list[str]) -> list[list[str]]:
    """Strip env assignments and wrappers like `timeout 60`; open up `sh -c '...'`."""
    while words:
        name = os.path.basename(words[0])
        if re.match(r"^\w+=", words[0]):  # FOO=bar git ...
            words = words[1:]
        elif name in WRAPPERS:
            rest, with_value = words[1:], WRAPPERS[name]
            while rest and (rest[0].startswith("-") or re.match(r"^\w+=", rest[0])):
                rest = rest[2:] if rest[0] in with_value else rest[1:]
            if name == "timeout" and rest:
                rest = rest[1:]  # the duration
            words = rest
        elif name in SHELLS and (
            flag := next((i for i, w in enumerate(words[1:-1], 1) if SHELL_C_RE.match(w)), 0)
        ):
            return segments(words[flag + 1])
        else:
            return [[name, *words[1:]]]
    return []


OPERATORS = "();<>|&\n"


def segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands, as word lists."""
    command = re.sub(r"(?<!\\)((?:\\\\)*)\\\r?\n", r"\1", command)  # line continuations
    # A "# comment" runs to the end of its line and eats the newline with it,
    # so a ";" after each newline still ends the command.
    command = command.replace("\n", "\n;")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=OPERATORS)
        lexer.whitespace, lexer.whitespace_split = " \t\r", True
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: split on the operators and words
        tokens = re.split(r"([;|&()\n]+)|\s+", command)
    words: list[list[str]] = []
    current: list[str] = []
    target = False  # the next word is a redirection's file, not an argument
    for token in [*tokens, ";"]:
        if not token:
            continue
        if set(token) <= set(OPERATORS):
            if set(token) & set(";|()\n") or token in ("&", "&&"):
                words += unwrap(current)
                current = []
            elif current and current[-1].isdigit():  # the 2 of 2>&1 is no argument
                current.pop()
            target = bool(set(token) & set("<>"))
            continue
        for inner in re.findall(r"\$\((.*)\)|`(.*)`", token):  # "$(git ...)" in quotes
            words += segments("".join(inner))
        if target:
            target = False
            continue
        current.append(token)
    return words


def positional(args: list[str], with_value: set[str]) -> list[str]:
    """Return the arguments that are not options or option values."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in with_value:
            skip = True
        elif not arg.startswith("-"):
            out.append(arg)
    return out


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
            else:
                cwd = os.path.join(cwd or os.getcwd(), value)
    for value in configs:
        key = value.split("=", 1)[0]
        if SIGNING_KEYS.search(key):
            return f"`-c {value}` changes commit signing. Commits must stay signed."
    if not args:
        return None
    sub, rest = args[0], args[1:]

    if sub == "config":
        words_ = positional(rest, CONFIG_OPTIONS_WITH_VALUE)
        action = words_.pop(0) if words_ and words_[0] in ("set", "unset", "get", "list") else ""
        reads = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
        writes = not reads & set(rest) and (
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
        if {"--all", "--mirror", "--branches"} & set(rest):
            return "Push one branch at a time; --all and --mirror push main too."
        refs = positional(rest, {"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
        if not any(a == "--repo" or a.startswith("--repo=") for a in rest):
            refs = refs[1:]  # the first one is the remote
        to_main = any(r.split(":")[-1] in ("main", "refs/heads/main") for r in refs)
        implicit = not refs or refs == ["HEAD"]  # pushes the current branch
        if to_main or (implicit and current_branch(cwd) == "main"):
            return "Never push to main; open a PR."
    return None


def main() -> int:
    """Exit 2 with the reason on stderr to block the command.

    Only exit 2 blocks, so a crash would let the command through: block then too.
    """
    try:
        data = json.load(sys.stdin)
        command = data.get("tool_input", {}).get("command", "")
        reasons = (problem(words, data.get("cwd")) for words in segments(command))
        reason = next((r for r in reasons if r), None)
    except Exception as err:  # noqa: BLE001
        reason = f"the check itself failed ({err!r}), so the command is blocked to be safe."
    if reason:
        sys.stderr.write(f"Blocked by .claude/hooks/guard-git.py: {reason}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
