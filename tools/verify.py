#!/usr/bin/env python3
"""One verification for every contribution, by people and agents alike.

Usage:
  verify.py                     everything before a PR: CI's checks, the branch and
                                the PR; prints the receipt for "How I tested this"
  verify.py branch              only the branch and PR checks (seconds, no tests)
  verify.py areas [--markdown]  what a change in each part of the repo must show
  verify.py commit-msg <file>   git commit-msg hook: conventional subject, no AI co-author
  verify.py pre-push            git pre-push hook: every commit to push is signed
  verify.py pr                  CI (PR Metadata): title, changelog, testing section and
                                commit signatures (reads PR_* and GH_TOKEN from env)

What each area needs lives in tools/verify-areas.toml; docs/VERIFYING.md
explains the whole flow.

Standard library only: the git hooks and CI run it without the project's
environment.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_notes

ROOT = Path(__file__).resolve().parents[1]
AREAS_FILE = ROOT / "tools" / "verify-areas.toml"
UPSTREAM_RE = re.compile(r"[:/]wealthystudent/ha-nanit(\.git)?/?$")
BRANCH_RE = re.compile(r"^(feat|fix|perf|refactor|docs|test|chore|ci|build|revert)/\S")
EXEMPT_SUBJECT_PREFIXES = ("fixup! ", "squash! ", "amend! ", "Merge ", 'Revert "')
AI_NAMES = (
    r"claude|anthropic|copilot|codex|openai|chatgpt|gpt-\d|gemini|cursor|devin|aider"
    r"|jules|windsurf|codeium|kiro|grok"
)
AI_TRAILER_RE = re.compile(rf"^co-authored-by:.*\b({AI_NAMES})", re.I | re.M)
AI_GENERATED_RE = re.compile(rf"generated (with|by)\b.*\b({AI_NAMES})", re.I)
RECEIPT_RE = re.compile(r"^ha-nanit verify ([0-9a-f]{7,40})\b", re.M)
RESULT_RE = re.compile(r"^result: (\w+)", re.M)
LIVE_RE = re.compile(r"^[*_ \t]*live test[*_ \t]*:[*_ \t]*(.*)$", re.I | re.M | re.S)
SIGNING_DOCS = "CONTRIBUTING.md#signed-commits"
STATUS_ORDER = ("fail", "warn", "ci", "skip", "pass")


@dataclass
class Check:
    """One line of the receipt."""

    status: str  # pass, fail, warn, skip (not needed here), ci (left to CI)
    name: str
    detail: str
    fix: str = ""


@dataclass
class Area:
    """A part of the repo and what a change to it must show."""

    name: str
    title: str
    paths: list[str]
    live: str
    hardware: str
    fallback: bool = False
    files: list[str] = field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────


def run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing stdout and stderr together."""
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
    )


def git(*args: str) -> str:
    """Run git and return stdout, or raise on failure."""
    result = run("git", *args)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def upstream_remote() -> str:
    """The remote that points at wealthystudent/ha-nanit (origin, or upstream in a fork)."""
    for line in git("remote", "-v").splitlines():
        name, url, *_ = line.split()
        if UPSTREAM_RE.search(url):
            return name
    return "origin"


def is_signed(sha: str) -> bool:
    """Whether a commit carries a signature. GitHub decides if it is valid."""
    header = git("cat-file", "commit", sha).split("\n\n", 1)[0]
    return any(line.startswith("gpgsig") for line in header.splitlines())


def ai_attribution(message: str) -> str | None:
    """Return the line that credits an AI tool, if any."""
    for line in message.splitlines():
        if AI_TRAILER_RE.match(line) or AI_GENERATED_RE.search(line):
            return line.strip()
    return None


def subject_error(subject: str) -> str | None:
    """Explain what is wrong with a commit subject, or None if it is fine."""
    if subject.startswith(EXEMPT_SUBJECT_PREFIXES):
        return None
    if not release_notes.TITLE_RE.match(subject):
        return (
            f"{subject!r} is not a conventional commit. Use '<type>: <what it does>', "
            "imperative and lowercase, e.g. 'fix: handle token refresh during reconnect'."
        )
    return None


def sign_fix(base_ref: str) -> str:
    """How to sign the commits of a branch you already pushed."""
    return (
        f"Set up signing ({SIGNING_DOCS}), then sign them in place and force-push:\n"
        f'  git rebase --force-rebase --gpg-sign "$(git merge-base HEAD {base_ref})"\n'
        "  git push --force-with-lease"
    )


# ─── Areas ────────────────────────────────────────────────────────────


def load_areas() -> list[Area]:
    """Read tools/verify-areas.toml."""
    data = tomllib.loads(AREAS_FILE.read_text())
    return [Area(**entry) for entry in data["area"]]


def areas_for(files: Iterable[str]) -> tuple[list[Area], list[str]]:
    """Group changed files by area; return the touched areas and unmapped files."""
    areas = load_areas()
    unmapped: list[str] = []
    for path in files:
        matched = [
            a
            for a in areas
            if not a.fallback and any(fnmatch.fnmatchcase(path, p) for p in a.paths)
        ] or [a for a in areas if a.fallback and any(fnmatch.fnmatchcase(path, p) for p in a.paths)]
        for area in matched:
            area.files.append(path)
        if not matched:
            unmapped.append(path)
    return [a for a in areas if a.files], unmapped


def areas_markdown() -> str:
    """The areas as the table in docs/VERIFYING.md."""
    lines = [
        "| Area | Needs | Live test must show |",
        "|------|-------|---------------------|",
    ]
    for area in load_areas():
        lines.append(f"| {area.title} (`{area.name}`) | {area.hardware} | {area.live} |")
    return "\n".join(lines) + "\n"


def needs_text(areas: list[Area], unmapped: list[str]) -> str:
    """What the PR's Live test must cover for these areas."""
    lines = []
    for area in areas:
        lines.append(f"- {area.title} [{area.hardware}]: {area.live}")
    if unmapped:
        lines.append(
            f"- Not in any area ({', '.join(unmapped)}): say how you verified it, and add "
            "the path to tools/verify-areas.toml."
        )
    return "\n".join(lines)


# ─── PR description ───────────────────────────────────────────────────


def live_text(testing: str) -> str:
    """The text after 'Live test:', up to the receipt."""
    match = LIVE_RE.search(testing)
    if not match:
        return ""
    text = match.group(1)
    for stop in ("```", "~~~", "ha-nanit verify "):
        text = text.split(stop, 1)[0]
    return text.strip()


def pr_body_problems(
    title: str, body: str, labels: list[str], author: str, shas: list[str]
) -> tuple[list[str], list[str]]:
    """Check a PR's title and description. Return (errors, warnings).

    shas are the PR's commits, oldest first, so the last is its head.
    """
    errors = release_notes.pr_errors(title, body, labels)
    warnings: list[str] = []
    if author.endswith("[bot]"):
        return errors, warnings

    if not release_notes.section_text(body, "AI use"):
        errors.append(
            "'## AI use' is missing or empty. Write 'none', or the tool and how you used it "
            "('assisted' or 'generated and reviewed')."
        )

    testing = release_notes.section_text(body, "How I tested this")
    if testing is None:
        errors.append("The description has no '## How I tested this' section. Use the PR template.")
        return errors, warnings

    if not live_text(testing):
        errors.append(
            "'Live test:' under '## How I tested this' is empty. Say what you ran against a "
            "real Home Assistant and what you saw, or write 'not tested live' and why. "
            "`just verify` lists what this change needs."
        )

    receipt = RECEIPT_RE.search(testing)
    if not receipt:
        errors.append(
            "'## How I tested this' has no `just verify` receipt. Run `just verify` on the "
            "PR's head and paste the block it prints, unchanged."
        )
        return errors, warnings
    sha = receipt.group(1)
    if shas and not any(s.startswith(sha) for s in shas):
        errors.append(
            f"The receipt is for commit {sha}, which is not in this PR. Run `just verify` "
            "on the PR's head and paste the new receipt."
        )
    elif shas and not shas[-1].startswith(sha):
        warnings.append(
            f"The receipt is for {sha}, an older commit of this PR. Run `just verify` again "
            "and replace it when the change is done."
        )
    result = RESULT_RE.search(testing[receipt.start() :])
    if not result or result.group(1) != "pass":
        errors.append("The receipt doesn't say 'result: pass'. Fix what failed and run it again.")
    return errors, warnings


# ─── Branch checks ────────────────────────────────────────────────────


def branch_checks(base_ref: str, commits: list[str]) -> list[Check]:
    """Checks on the commits and git setup, shared by `verify` and `verify branch`."""
    checks: list[Check] = []

    dirty = git("status", "--porcelain", "--untracked-files=normal")
    if dirty:
        checks.append(
            Check(
                "fail",
                "tree",
                f"{len(dirty.splitlines())} uncommitted or untracked files",
                "Commit them (or add them to .gitignore): the receipt must describe exactly "
                "what you push.",
            )
        )
    else:
        checks.append(Check("pass", "tree", "clean, so the receipt matches HEAD"))

    if not commits:
        checks.append(
            Check(
                "fail",
                "commits",
                f"no commits on top of {base_ref}",
                "Work on a branch: git switch -c fix/<what-it-fixes>",
            )
        )
        return checks

    unsigned, ai_lines, bad_subjects = [], [], []
    for sha in commits:
        message = git("log", "-1", "--format=%B", sha)
        subject = message.splitlines()[0] if message else ""
        if not is_signed(sha):
            unsigned.append(f"{sha[:8]} {subject}")
        if line := ai_attribution(message):
            ai_lines.append(f"{sha[:8]}: {line}")
        if subject_error(subject):
            bad_subjects.append(f"{sha[:8]} {subject}")

    n = len(commits)
    if unsigned:
        checks.append(
            Check(
                "fail",
                "signed",
                f"{len(unsigned)} of {n} commits unsigned: " + "; ".join(unsigned),
                sign_fix(base_ref) + "\nmain only accepts PRs whose commits are all signed.",
            )
        )
    else:
        checks.append(Check("pass", "signed", f"all {n} commits signed"))
    if ai_lines:
        checks.append(
            Check(
                "fail",
                "credit",
                "AI attribution in " + "; ".join(ai_lines),
                "Remove AI co-author trailers and 'generated with' lines (git rebase -i, "
                "reword). Say how you used AI in the PR description instead.",
            )
        )
    else:
        checks.append(Check("pass", "credit", "no AI co-author trailers"))
    if bad_subjects:
        checks.append(
            Check(
                "warn",
                "messages",
                "not conventional: " + "; ".join(bad_subjects),
                "Only the PR title reaches main, but keep commits '<type>: <what>' for review.",
            )
        )
    else:
        checks.append(Check("pass", "messages", "conventional commit subjects"))

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if not BRANCH_RE.match(branch):
        checks.append(
            Check(
                "warn",
                "branch",
                f"{branch!r} has no type prefix",
                "Name branches feat/, fix/, refactor/, docs/, test/ or chore/ + a description.",
            )
        )

    if git_config("commit.gpgsign") != "true":
        checks.append(
            Check(
                "warn",
                "signing",
                "commit.gpgsign is off, so your next commits won't be signed",
                f"Set up signing: {SIGNING_DOCS}",
            )
        )

    hooks = Path(git("rev-parse", "--git-path", "hooks"))
    if not hooks.is_absolute():
        hooks = ROOT / hooks
    missing = [h for h in ("commit-msg", "pre-push") if not (hooks / h).exists()]
    if missing:
        checks.append(
            Check(
                "warn",
                "hooks",
                f"git hooks missing: {', '.join(missing)}",
                "Run `just setup` to install them.",
            )
        )
    return checks


def git_config(key: str) -> str:
    """A git config value, or '' when unset."""
    return run("git", "config", "--get", key).stdout.strip()


def pr_check(commits: list[str]) -> list[Check]:
    """Check the open PR of this branch, if there is one and gh can see it."""
    if not shutil.which("gh"):
        return [Check("skip", "pr", "gh not installed; CI checks the description")]
    result = run("gh", "pr", "view", "--json", "number,title,body,labels,author,headRefOid,state")
    if result.returncode != 0:
        return [Check("skip", "pr", "no PR for this branch yet; run again after opening it")]
    pr = json.loads(result.stdout)
    if pr["state"] != "OPEN":
        return [Check("skip", "pr", f"#{pr['number']} is {pr['state'].lower()}")]
    errors, warnings = pr_body_problems(
        pr["title"],
        pr["body"] or "",
        [label["name"] for label in pr["labels"]],
        pr["author"]["login"],
        commits,
    )
    head = git("rev-parse", "HEAD")
    if pr["headRefOid"] != head:
        warnings.append(
            f"#{pr['number']} is at {pr['headRefOid'][:12]}, you are at {head[:12]}: push."
        )
    # The receipt you are about to paste can't be in the description yet.
    first_run = [e for e in errors if "no `just verify` receipt" in e]
    errors = [e for e in errors if e not in first_run]
    warnings += ["Paste this receipt into '## How I tested this'." for _ in first_run]
    if errors:
        return [Check("fail", "pr", f"#{pr['number']}: " + " ".join(errors), " ".join(warnings))]
    if warnings:
        return [Check("warn", "pr", f"#{pr['number']}: " + " ".join(warnings))]
    return [Check("pass", "pr", f"#{pr['number']} title, changelog and testing section")]


# ─── Code checks (what CI runs) ───────────────────────────────────────


def _tail(output: str, lines: int = 40) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


def code_checks(areas: list[Area]) -> list[Check]:
    """Run what CI runs, as far as this machine can."""
    checks: list[Check] = []
    touched = {a.name for a in areas}

    lock = run("uv", "lock", "--check")
    checks.append(
        Check("pass", "lock", "uv.lock matches pyproject.toml")
        if lock.returncode == 0
        else Check("fail", "lock", "uv.lock is out of date", "Run `uv lock` and commit it.")
    )

    lint = [
        run("uv", "run", "ruff", "check", "."),
        run("uv", "run", "ruff", "format", "--check", "."),
    ]
    failed = [r for r in lint if r.returncode != 0]
    checks.append(
        Check("fail", "lint", _tail(failed[0].stdout + failed[0].stderr), "Run `just fix`.")
        if failed
        else Check("pass", "lint", "ruff check and format")
    )

    mypy = run("uv", "run", "mypy")
    checks.append(
        Check("pass", "types", "mypy strict")
        if mypy.returncode == 0
        else Check("fail", "types", _tail(mypy.stdout), "Fix the types; never `# type: ignore`.")
    )

    tests = run(
        "uv", "run", "pytest", "tests/unit/", "-q", "-p", "no:cacheprovider",
        "--cov=custom_components/nanit", "--cov-fail-under=80",
    )  # fmt: skip
    out = tests.stdout + tests.stderr
    passed = re.findall(r"(\d+) passed", out)
    coverage = re.search(r"Total coverage: ([\d.]+)%", out)
    summary = f"integration {passed[-1] if passed else '?'} passed"
    if coverage:
        summary += f", {float(coverage.group(1)):.1f}% coverage"
    checks.append(
        Check("pass", "tests", summary)
        if tests.returncode == 0
        else Check("fail", "tests", _tail(out), "Fix the failing tests (coverage floor is 80%).")
    )

    lib = run("uv", "run", "pytest", "packages/aionanit/tests/", "-q", "-p", "no:cacheprovider")
    out = lib.stdout + lib.stderr
    passed = re.findall(r"(\d+) passed", out)
    checks.append(
        Check("pass", "library", f"aionanit {passed[-1] if passed else '?'} passed")
        if lib.returncode == 0
        else Check("fail", "library", _tail(out), "Fix the failing aionanit tests.")
    )

    checks.append(card_check("card" in touched))
    checks.append(protobuf_check("protobuf" in touched or "sound-light" in touched))
    checks.append(Check("ci", "ci-only", "hassfest and workflow lint run in CI only"))
    return checks


def card_check(touched: bool) -> Check:
    """Rebuild the card and compare it with the committed bundle."""
    if not touched:
        return Check("skip", "card", "card not touched")
    if not shutil.which("npm"):
        return Check("fail", "card", "npm not found", "Install Node.js 24 (frontend/.nvmrc).")
    frontend = ROOT / "frontend"
    for step in (
        ("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"),
        ("npm", "run", "build"),
    ):
        result = run(*step, cwd=frontend)
        if result.returncode != 0:
            return Check("fail", "card", _tail(result.stdout + result.stderr), "Fix the build.")
    bundle = "custom_components/nanit/frontend/nanit-card.js"
    if run("git", "diff", "--quiet", "--", bundle).returncode != 0:
        run("git", "checkout", "--", bundle)
        return Check(
            "fail",
            "card",
            "nanit-card.js doesn't match frontend/src",
            "Run `just card` and commit.",
        )
    wanted = (frontend / ".nvmrc").read_text().strip().lstrip("v").split(".")[0]
    have = run("node", "--version").stdout.strip().lstrip("v").split(".")[0]
    note = "" if wanted == have else f" (built with Node {have}, CI uses {wanted})"
    return Check("pass", "card", "bundle matches frontend/src" + note)


def protobuf_check(touched: bool) -> Check:
    """Regenerate the S&L protobuf module and compare, like CI's drift check."""
    if not touched:
        return Check("skip", "protobuf", "no protobuf change")
    protoc = run("protoc", "--version") if shutil.which("protoc") else None
    if not protoc or "29.5" not in protoc.stdout:
        return Check("ci", "protobuf", "protoc 29.5 not found here; CI's drift check runs it")
    sl = "custom_components/nanit/aionanit_sl"
    run(
        "protoc",
        f"--python_out={sl}",
        f"--pyi_out={sl}",
        f"--proto_path={sl}",
        f"{sl}/sound_light.proto",
    )
    if run("git", "diff", "--quiet", "--", sl).returncode != 0:
        run("git", "checkout", "--", sl)
        return Check(
            "fail",
            "protobuf",
            "sound_light_pb2 doesn't match the .proto",
            "Regenerate with protoc 29.5.",
        )
    return Check("pass", "protobuf", "generated code matches the .proto")


def versions() -> str:
    """Tool versions for the receipt."""
    uv = run("uv", "--version").stdout.split()[1:2]
    py = run("uv", "run", "python", "-c", "import platform; print(platform.python_version())")
    ha = run(
        "uv", "run", "python", "-c",
        "from homeassistant.const import __version__; print(__version__)",
    )  # fmt: skip
    parts = [f"uv {uv[0] if uv else '?'}", f"Python {py.stdout.strip() or '?'}"]
    parts.append(f"Home Assistant {ha.stdout.strip() or '?'}")
    parts.append(sys.platform)
    return " · ".join(parts)


# ─── Commands ─────────────────────────────────────────────────────────


def context() -> tuple[str, str, list[str], list[str]]:
    """Return (base ref, merge base, commits oldest first, changed files)."""
    remote = upstream_remote()
    base_ref = f"{remote}/main"
    run("git", "fetch", "--quiet", remote, "main")
    merge_base = git("merge-base", base_ref, "HEAD")
    commits = git("rev-list", "--reverse", f"{merge_base}..HEAD").split()
    files = git("diff", "--name-only", f"{merge_base}...HEAD").split()
    return base_ref, merge_base, commits, files


def print_checks(checks: list[Check]) -> None:
    for check in checks:
        detail = check.detail if "\n" not in check.detail else check.detail.splitlines()[0]
        print(f"  {check.status:<5} {check.name:<9} {detail}")


def print_fixes(checks: list[Check]) -> None:
    problems = [c for c in checks if c.status in ("fail", "warn") and (c.fix or "\n" in c.detail)]
    if not problems:
        return
    print("\nTo fix:")
    for check in problems:
        print(f"\n[{check.status}] {check.name}")
        if "\n" in check.detail:
            print(check.detail)
        if check.fix:
            print(check.fix)


def cmd_verify(*, full: bool) -> int:
    base_ref, merge_base, commits, files = context()
    areas, unmapped = areas_for(files)
    checks = code_checks(areas) if full else []
    checks += branch_checks(base_ref, commits)
    checks += pr_check(commits)
    result = "fail" if any(c.status == "fail" for c in checks) else "pass"

    head = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if full:
        print("Paste this block, unchanged, into '## How I tested this':\n")
        print("```text")
        print(
            f"ha-nanit verify {head[:12]} on {branch} "
            f"({len(commits)} commits on {base_ref} {merge_base[:8]})"
        )
    else:
        print(f"Branch checks for {head[:12]} on {branch} (no receipt: run `just verify`)")
    print_checks(checks)
    print(f"areas: {', '.join(a.name for a in areas) or 'none'}")
    if full:
        print(f"result: {result}")
        print(versions())
        print("```")
    print("\nLive test: what this change must show (write it under 'Live test:'):")
    print(needs_text(areas, unmapped) or "- Nothing beyond the checks above.")
    print_fixes(checks)
    return 0 if result == "pass" else 1


def cmd_commit_msg(path: str) -> int:
    lines = [ln for ln in Path(path).read_text().splitlines() if not ln.startswith("#")]
    message = "\n".join(lines).strip()
    if not message:
        return 0
    problems = []
    if error := subject_error(message.splitlines()[0]):
        problems.append(error)
    if line := ai_attribution(message):
        problems.append(
            f"{line!r} credits an AI tool. ha-nanit commits carry no AI co-author trailers or "
            "'generated with' lines; say how you used AI in the PR description instead."
        )
    for problem in problems:
        print(f"commit-msg: {problem}", file=sys.stderr)
    if problems:
        print(f"Your message is kept in {path}; fix it and commit again.", file=sys.stderr)
    return 1 if problems else 0


def cmd_pre_push() -> int:
    to_ref = os.environ.get("PRE_COMMIT_TO_REF", "")
    from_ref = os.environ.get("PRE_COMMIT_FROM_REF", "")
    if not to_ref or set(to_ref) == {"0"}:
        return 0
    base_ref = f"{upstream_remote()}/main"
    span = [to_ref] if not from_ref or set(from_ref) == {"0"} else [f"{from_ref}..{to_ref}"]
    not_main = (
        ["--not", base_ref]
        if run("git", "rev-parse", "--verify", "-q", base_ref).returncode == 0
        else []
    )
    commits = git("rev-list", *span, *not_main).split()
    unsigned = [c for c in commits if not is_signed(c)]
    credited = [c for c in commits if ai_attribution(git("log", "-1", "--format=%B", c))]
    for sha in unsigned:
        print(f"pre-push: {git('log', '-1', '--format=%h %s', sha)} is not signed", file=sys.stderr)
    for sha in credited:
        print(
            f"pre-push: {git('log', '-1', '--format=%h %s', sha)} credits an AI tool",
            file=sys.stderr,
        )
    if unsigned:
        print(sign_fix(base_ref), file=sys.stderr)
    return 1 if unsigned or credited else 0


def _api(url: str, token: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


SIGNATURE_HINTS = {
    "unsigned": "it isn't signed",
    "unknown_key": "its key isn't on the author's GitHub account as a signing key",
    "no_user": "its email isn't on any GitHub account",
    "unverified_email": "its email isn't verified on the author's GitHub account",
    "bad_email": "its email doesn't match the signing key",
    "expired_key": "its key has expired",
}


def cmd_pr() -> int:
    """PR Metadata in CI. Everything comes from the event, via env."""
    env = os.environ
    repo, number, token = env["GITHUB_REPOSITORY"], env["PR_NUMBER"], env["GH_TOKEN"]
    base = f"https://api.github.com/repos/{repo}/pulls/{number}"
    commits: list[dict[str, object]] = []
    for page in range(1, 4):  # GitHub lists at most 250 commits
        batch = _api(f"{base}/commits?per_page=100&page={page}", token)
        assert isinstance(batch, list)
        commits += batch
        if len(batch) < 100:
            break
    files: list[str] = []
    for page in range(1, 31):
        batch = _api(f"{base}/files?per_page=100&page={page}", token)
        assert isinstance(batch, list)
        files += [f["filename"] for f in batch]
        if len(batch) < 100:
            break

    errors, warnings = pr_body_problems(
        env.get("PR_TITLE", ""),
        env.get("PR_BODY", ""),
        json.loads(env.get("PR_LABELS", "[]")),
        env.get("PR_AUTHOR", ""),
        [str(c["sha"]) for c in commits],
    )
    unverified = []
    for c in commits:
        commit = c["commit"]
        assert isinstance(commit, dict)
        verification = commit["verification"]
        subject = str(commit["message"]).splitlines()[0]
        if not verification["verified"]:
            reason = SIGNATURE_HINTS.get(verification["reason"], verification["reason"])
            unverified.append(f"{str(c['sha'])[:8]} {subject!r}: {reason}")
        if line := ai_attribution(str(commit["message"])):
            errors.append(f"Commit {str(c['sha'])[:8]} credits an AI tool ({line!r}). Remove it.")
    if unverified:
        errors.append(
            f"main only accepts signed commits, and {len(unverified)} of {len(commits)} here "
            "aren't verified by GitHub: " + "; ".join(unverified) + f". See {SIGNING_DOCS} "
            "for setup and how to sign a branch you already pushed (`just verify branch` checks it "
            "locally)."
        )

    areas, unmapped = areas_for(files)
    if summary := env.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as out:
            out.write("### What this PR must show under Live test\n\n")
            out.write((needs_text(areas, unmapped) or "Nothing beyond CI.") + "\n")
    for warning in warnings:
        print(f"::warning::{warning}")
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print("Title, changelog, testing section and commit signatures look good.")
    print("\nAreas this PR touches: " + (", ".join(a.name for a in areas) or "none"))
    print(needs_text(areas, unmapped))
    return 1 if errors else 0


def main(argv: list[str]) -> int:
    """Entry point."""
    match argv:
        case []:
            return cmd_verify(full=True)
        case ["branch"]:
            return cmd_verify(full=False)
        case ["areas"]:
            for area in load_areas():
                print(f"{area.name} [{area.hardware}]: {area.live}")
            return 0
        case ["areas", "--markdown"]:
            print(areas_markdown(), end="")
            return 0
        case ["commit-msg", path]:
            return cmd_commit_msg(path)
        case ["pre-push"]:
            return cmd_pre_push()
        case ["pr"]:
            return cmd_pr()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
