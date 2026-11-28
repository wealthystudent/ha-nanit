"""Tests for tools/verify.py and the Claude Code git guard."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verify = _load("verify", ROOT / "tools" / "verify.py")
guard = _load("guard_git", ROOT / ".claude" / "hooks" / "guard-git.py")

HEAD = "a" * 40
OLDER = "b" * 40
RECEIPT = f"""```text
ha-nanit verify {HEAD[:12]} on fix/x (2 commits on origin/main 12345678)
  pass  tests     integration 960 passed
areas: entities
result: pass
```"""


def body(live: str = "Ran it in just dev on HA 2026.10.4; the sensor shows 22 °C.") -> str:
    return f"""## What does this do

Fixes the thing.

## Changelog

The temperature sensor no longer shows -1000.

## How I tested this

**Live test:** {live}

{RECEIPT}

## AI use

assisted (Claude Code)
"""


def problems(text: str, *, author: str = "someone", shas: list[str] | None = None):
    return verify.pr_body_problems(
        "fix: x", text, [], author, [OLDER, HEAD] if shas is None else shas
    )


# ─── Areas ────────────────────────────────────────────────────────────


def _names(files: list[str]) -> list[str]:
    areas, unmapped = verify.areas_for(files)
    return [a.name for a in areas] + [f"unmapped:{f}" for f in unmapped]


def test_areas_match_by_path() -> None:
    assert _names(["frontend/src/nanit-card.ts"]) == ["card"]
    assert _names(["custom_components/nanit/aionanit_sl/transport.py"]) == ["sound-light"]
    assert _names(["packages/aionanit/aionanit/camera.py"]) == ["streaming", "connection"]


def test_fallback_areas_only_cover_what_nothing_else_does() -> None:
    assert _names(["packages/aionanit/aionanit/models.py"]) == ["library"]
    assert _names(["README.md"]) == ["docs"]
    assert _names(["tests/unit/test_hub.py"]) == ["tests"]
    assert _names(["AGENTS.md"]) == ["agent-rules"]
    assert _names(["custom_components/nanit/sensor.py", "README.md"]) == ["entities", "docs"]


def test_unmapped_files_are_reported() -> None:
    assert _names(["LICENSE"]) == ["unmapped:LICENSE"]
    assert "tools/verify-areas.toml" in verify.needs_text([], ["LICENSE"])


def test_every_tracked_file_has_an_area() -> None:
    files = verify.git("ls-files").splitlines()
    _, unmapped = verify.areas_for(files)
    assert set(unmapped) <= {"LICENSE"}


def test_docs_table_matches_the_areas_file() -> None:
    doc = (ROOT / "docs" / "VERIFYING.md").read_text()
    table = doc.split("<!-- areas:start", 1)[1].split("-->", 1)[1].split("<!-- areas:end -->")[0]
    assert table.strip() == verify.areas_markdown().strip(), (
        "Regenerate the table in docs/VERIFYING.md with `just verify areas --markdown`."
    )


# ─── PR description ───────────────────────────────────────────────────


def test_complete_description_passes() -> None:
    assert problems(body()) == ([], [])


def test_template_fails_with_every_missing_part() -> None:
    template = (ROOT / ".github" / "pull_request_template.md").read_text()
    errors, warnings = verify.pr_body_problems("chore: x", template, [], "someone", [HEAD])
    text = " ".join(errors)
    assert "'## AI use' is missing or empty" in text
    assert "'Live test:'" in text
    assert any("no `just verify` receipt" in w for w in warnings)


def test_live_test_must_say_something() -> None:
    errors, _ = problems(body(live=""))
    assert any("'Live test:'" in e for e in errors)
    assert problems(body(live="not tested live: no camera here"))[0] == []


@pytest.mark.parametrize(
    "label",
    [
        "- Live test: ran it",
        "> Live test: ran it",
        "- **Live test:** ran it",
        "### Live test\n\nran it",
    ],
)
def test_live_test_label_forms(label: str) -> None:
    assert problems(body().replace("**Live test:** ", label + "\n\n", 1))[0] == []


def test_filled_in_template_passes() -> None:
    template = (ROOT / ".github" / "pull_request_template.md").read_text()
    filled = (
        template.replace("## Changelog\n", "## Changelog\n\nThe sensor shows the right value.\n")
        .replace("## AI use\n", "## AI use\n\nnone\n")
        .replace("unchanged. -->\n", "unchanged. -->\n\n" + RECEIPT + "\n")
    )
    assert any("'Live test:'" in e for e in problems(filled)[0])
    # Written under the template's comment, as prose or as a list.
    for live in ("Ran it in just dev; the sensor shows 22 °C.", "- just dev\n- sensor shows 22 °C"):
        text = filled.replace("\n-->\n\n<!-- Run", f"\n-->\n\n{live}\n\n<!-- Run", 1)
        assert text != filled
        assert problems(text)[0] == [], live


def test_empty_live_test_is_not_filled_by_a_later_line() -> None:
    errors, _ = problems(body(live="\n\nScreenshots below."))
    assert any("'Live test:'" in e for e in errors)
    errors, _ = problems(body().replace("**Live test:** ", "### Live test\n\n### Notes\n"))
    assert any("'Live test:'" in e for e in errors)


def test_receipt_from_another_commit_only_warns() -> None:
    errors, warnings = problems(body(), shas=["c" * 40])
    assert errors == []
    assert any("not in this PR" in w for w in warnings)


def test_missing_receipt_only_warns() -> None:
    errors, warnings = problems(body().replace(RECEIPT, ""))
    assert errors == []
    assert any("no `just verify` receipt" in w for w in warnings)


def test_receipt_for_an_older_commit_warns() -> None:
    errors, warnings = problems(body(), shas=[HEAD, OLDER])
    assert errors == []
    assert any("older commit" in w for w in warnings)


@pytest.mark.parametrize("result", ["result: fail", "result: pass-with-errors", ""])
def test_receipt_must_say_pass_exactly(result: str) -> None:
    errors, _ = problems(body().replace("result: pass", result))
    assert any("result: pass" in e for e in errors)


def test_result_outside_the_receipt_block_does_not_count() -> None:
    text = body().replace("result: pass\n```", "```\n\nresult: pass")
    errors, _ = problems(text)
    assert any("result: pass" in e for e in errors)


def test_live_label_inside_a_code_block_does_not_count() -> None:
    text = body(live="").replace(RECEIPT, "```\nLive test: fake\n```\n" + RECEIPT)
    errors, _ = problems(text)
    assert any("'Live test:'" in e for e in errors)


def test_receipt_must_be_in_a_closed_code_block() -> None:
    unfenced = body().replace("```text\n", "").replace("result: pass\n```", "result: pass")
    assert any("no `just verify` receipt" in w for w in problems(unfenced)[1])
    unclosed = body().replace("result: pass\n```", "result: pass")
    assert any("no `just verify` receipt" in w for w in problems(unclosed)[1])


def test_longer_fences_work() -> None:
    text = body().replace("```text", "````text").replace("result: pass\n```", "result: pass\n````")
    assert problems(text) == ([], [])
    hidden = body(live="").replace(RECEIPT, "````\nLive test: fake\n```\n````\n" + RECEIPT)
    assert any("'Live test:'" in e for e in problems(hidden)[0])


def test_local_run_only_warns_about_a_failed_receipt() -> None:
    failed = body().replace("result: pass", "result: fail")
    errors, warnings = verify.pr_body_problems(
        "fix: x", failed, [], "someone", ["c" * 40], new_receipt=True
    )
    assert errors == []
    assert any("not in this PR" in w and "replace it" in w for w in warnings)
    assert any("result: pass" in w for w in warnings)


def test_bots_skip_the_written_sections() -> None:
    assert verify.pr_body_problems("chore(deps): bump x", "", [], "dependabot[bot]", []) == (
        [],
        [],
    )


def test_older_prs_skip_the_written_sections() -> None:
    old_body = "## Changelog\n\nThe sensor works.\n"
    legacy = verify.LEGACY_LAST_PR
    assert verify.pr_body_problems("fix: x", old_body, [], "someone", [], number=legacy) == ([], [])
    assert verify.pr_body_problems("fix: x", old_body, [], "someone", [], number=legacy + 1)[0]


def test_title_and_changelog_rules_still_apply() -> None:
    errors, _ = verify.pr_body_problems("Fix stuff", body(), [], "someone", [HEAD])
    assert any("conventional commit" in e for e in errors)


# ─── Commits ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Co-authored-by: Claude Opus 5 <claude@example.com>",
        "Co-authored-by: Claude Sonnet 4.5 <noreply@example.com>",
        "Co-authored-by: Sisyphus <clio-agent@sisyphuslabs.ai>",
        "Ultraworked with [Sisyphus](https://github.com/code-yeongyu/oh-my-opencode)",
        "Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
        "co-authored-by: codex <codex@openai.com>",
        "Co-authored-by: Cursor Agent <cursoragent@cursor.com>",
        "Co-authored-by: chatgpt-codex-connector[bot] <199175422+chatgpt-codex-connector[bot]@users.noreply.github.com>",
        "Co-authored-by: devin-ai-integration[bot] <158243242+devin-ai-integration[bot]@users.noreply.github.com>",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    ],
)
def test_ai_attribution_is_found(line: str) -> None:
    assert verify.ai_attribution(f"fix: x\n\nBody.\n\n{line}\n") == line


@pytest.mark.parametrize(
    "message",
    [
        "fix: x\n\nCo-authored-by: Jordan <com6056@gmail.com>",
        "fix: x\n\nCo-authored-by: Claudette Smith <claudette@example.com>",
        "fix: x\n\nCo-authored-by: Claude Monet <claude@example.com>",
        "fix: x\n\nCo-authored-by: Devin Smith <devin@example.com>",
        "fix: x\n\nCo-authored-by: Jules Martin <jules@example.org>",
        "fix: x\n\nCo-authored-by: Sam Altwork <sam@openai.com>",
        "fix: x\n\nCo-authored-by: Dario <dario@anthropic.com>",
        "docs: explain diagnostics generated with Cursor",
        "fix: x\n\nThe old card was generated with a script, not by Claude.",
    ],
)
def test_no_false_ai_attribution(message: str) -> None:
    assert verify.ai_attribution(message) is None


@pytest.mark.parametrize(
    ("subject", "ok"),
    [
        ("fix: handle token refresh", True),
        ("feat(aionanit)!: drop python 3.12", True),
        ("fixup! fix: handle token refresh", True),
        ("Merge branch 'main' into fix/x", True),
        ("Fixed the thing", False),
        ("wip", False),
    ],
)
def test_subjects(subject: str, ok: bool) -> None:
    assert (verify.subject_error(subject) is None) is ok


def test_commit_msg_hook(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("fix: handle token refresh\n\n# Please enter the commit message\n")
    assert verify.cmd_commit_msg(str(msg)) == 0
    msg.write_text("fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
    assert verify.cmd_commit_msg(str(msg)) == 1
    assert "credits an AI tool" in capsys.readouterr().err
    msg.write_text("updated stuff\n")
    assert verify.cmd_commit_msg(str(msg)) == 1


def test_is_signed_reads_the_signature_header(monkeypatch: pytest.MonkeyPatch) -> None:
    header = "tree 1\nauthor a <a@b> 1 +0000\ncommitter a <a@b> 1 +0000\n"
    signed = header + "gpgsig -----BEGIN SSH SIGNATURE-----\n x\n -----END SSH SIGNATURE-----\n"
    commits = {"s": signed + "\nfix: x", "u": header + "\nfix: x\n\ngpgsig in the body"}
    monkeypatch.setattr(verify, "git", lambda *args: commits[args[-1]])
    assert verify.is_signed("s")
    assert not verify.is_signed("u")


# ─── CI (PR Metadata) ─────────────────────────────────────────────────


def _commit(sha: str, verified: bool, reason: str = "valid", message: str = "fix: x") -> dict:
    return {
        "sha": sha,
        "commit": {"message": message, "verification": {"verified": verified, "reason": reason}},
    }


def _run_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    commits: list[dict],
    text: str,
    number: str = "300",
) -> int:
    def fake_api(url: str, token: str) -> list:
        return commits if "/commits" in url else [{"filename": "custom_components/nanit/sensor.py"}]

    monkeypatch.setattr(verify, "_api", fake_api)
    for key, value in {
        "GITHUB_REPOSITORY": "wealthystudent/ha-nanit",
        "PR_NUMBER": number,
        "GH_TOKEN": "t",
        "PR_TITLE": "fix: x",
        "PR_BODY": text,
        "PR_LABELS": json.dumps([]),
        "PR_AUTHOR": "someone",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }.items():
        monkeypatch.setenv(key, value)
    return int(verify.cmd_pr())


def test_ci_passes_a_signed_complete_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_pr(monkeypatch, tmp_path, [_commit(OLDER, True), _commit(HEAD, True)], body()) == 0
    assert "Entities and their states" in (tmp_path / "summary.md").read_text()


def test_ci_names_unverified_commits_and_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commits = [_commit(OLDER, False, "unsigned"), _commit(HEAD, False, "unknown_key")]
    assert _run_pr(monkeypatch, tmp_path, commits, body()) == 1
    out = capsys.readouterr().out
    assert "2 of 2" in out
    assert "it isn't signed" in out
    assert "isn't on the author's GitHub account as a signing key" in out


def test_ci_fails_when_github_lists_fewer_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PR_COMMITS", "300")
    assert _run_pr(monkeypatch, tmp_path, [_commit(OLDER, True), _commit(HEAD, True)], body()) == 1
    assert "only 2 of this PR's 300 commits" in capsys.readouterr().out


def test_ci_fails_when_the_head_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PR_HEAD_SHA", "d" * 40)
    assert _run_pr(monkeypatch, tmp_path, [_commit(OLDER, True), _commit(HEAD, True)], body()) == 1
    assert "moved to a new head" in capsys.readouterr().out


def test_ci_holds_older_prs_to_the_old_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old_body = "## Changelog\n\nThe sensor works.\n"
    commits = [_commit(HEAD, True)]
    assert _run_pr(monkeypatch, tmp_path, commits, old_body, str(verify.LEGACY_LAST_PR)) == 0
    assert _run_pr(monkeypatch, tmp_path, commits, old_body) == 1
    unsigned = [_commit(HEAD, False, "unsigned")]
    assert _run_pr(monkeypatch, tmp_path, unsigned, old_body, "1") == 1
    assert "it isn't signed" in capsys.readouterr().out


def test_ci_rejects_ai_trailers_in_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trailer = "fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
    assert _run_pr(monkeypatch, tmp_path, [_commit(HEAD, True, message=trailer)], body()) == 1
    assert "credits an AI tool" in capsys.readouterr().out


# ─── Claude Code git guard ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("git push --force-with-lease", False),
        ("git push origin fix/x --force-with-lease=fix/x:abc123", False),
        ('git rebase --force-rebase --gpg-sign "$(git merge-base HEAD origin/main)"', False),
        ("git config --get commit.gpgsign", False),
        ("git config get commit.gpgsign", False),
        ("git push -u origin HEAD", False),
        ("git push origin fix/x > push.log 2>&1 | tail -3", False),
        ("git config --unset commit.gpgsign", True),
        ("git config set commit.gpgsign false", True),
        ("git config unset user.signingkey", True),
        ("git config --file .git/config commit.gpgsign false", True),
        ("git config -f x.cfg --get commit.gpgsign", False),
        ("git config --file x.cfg --get commit.gpgsign true", False),
        ("git push --force", True),
        ("git push -uf origin fix/x", True),
        ("git push origin +fix/x", True),
        ("git push origin HEAD:main", True),
        ("git push >/dev/null origin main", True),
        ("git push --repo origin main", True),
        ("git push --repo=origin main", True),
        ("git push --all origin", True),
        ("git push origin HEAD \\\n  --force", True),
        ("git push origin fix/x # publish it\ngit status", False),
        ("git push origin `git rev-parse HEAD`:main", True),
        ("git push origin HEAD >$(git rev-parse --show-toplevel)/push.log --force", True),
        ("git push origin >`git rev-parse --show-toplevel`/push.log main", True),
        ("git commit -m 'docs: describe `git push --force` in the guard'", False),
        ("git commit -m 'docs: $(git push --force) is blocked'", False),
        ('git commit -m "fix: x" -m "from `git rev-parse HEAD`"', False),
        ("git commit -m \"$(cat <<'EOF'\ndocs: describe `git push --force`\nEOF\n)\"", False),
        ("git commit -m \"$(cat <<'EOF'\nfix: keep the user's session\nEOF\n)\" --no-verify", True),
        (
            "git commit -F - <<'EOF'\ndocs: `git push --force` isn't run\nEOF\ngit push --force",
            True,
        ),
        ("git push origin fix/x # don't push main", False),
        ("git commit -F - <<EOF\ndocs: don't run `git push --force`\nEOF", True),
        ('git commit -F - <<EOF\nfix: keep the user\'s "session" # 12\nEOF', False),
        ("git push origin $(git rev-parse HEAD):fix/x", False),
        ("if git diff --quiet; then git push --force origin fix/x; fi", True),
        ("while true; do git commit --no-verify -m x; done", True),
        ("git grep -n -- '--no-verify'", False),
        ("git log --oneline --grep '--no-gpg-sign'", False),
        ("git -c commit.gpgsign=false commit -m 'fix: x'", True),
        ("git config commit.gpgsign false", True),
        ("cd x && git commit -n -m 'fix: x'", True),
        ("git commit --no-gpg-sign -m 'fix: x'", True),
    ],
)
def test_guard(command: str, blocked: bool, tmp_path: Path) -> None:
    import subprocess

    # On a feature branch, so implicit pushes pass wherever the tests run.
    subprocess.run(["git", "init", "-q", "-b", "fix/x", str(tmp_path)], check=True)
    reasons = [guard.problem(words, str(tmp_path)) for words in guard.segments(command)]
    assert any(reasons) is blocked


@pytest.mark.parametrize(
    "command",
    [
        "timeout 120 git push --force",
        "timeout -k 5 60 git push --force",
        "env git push --force",
        "env -u X FOO=1 git push --force",
        "nice -n 5 git push --force",
        "/usr/bin/git push --force",
        "sh -c 'git push --force'",
        "sh -c 'git push --force && echo done'",
        "bash -lc 'git push --force origin feat'",
        "sh -xc 'git commit --no-verify -m x'",
        'git status >"$(git push --force origin feat)"',
        "git status >`git push --force origin feat`",
        "echo `git push --force`",
        "git status; timeout 60 git push --force",
        "git status # check the branch\ngit push --force",
        'bash -c "cd x && git push origin +fix/x"',
        "(git push --force)",
        'echo "$(git push --force)"',
    ],
)
def test_guard_sees_through_wrappers(command: str) -> None:
    assert any(guard.problem(words, "/") for words in guard.segments(command)), command


def test_guard_blocks_when_it_cannot_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    missing = tmp_path / "missing"
    event = {"tool_input": {"command": f"git -C {missing} push"}, "cwd": str(tmp_path)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert guard.main() == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert guard.main() == 2
    assert "check itself failed" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_input": {"command": "ls"}})))
    assert guard.main() == 0


def test_guard_blocks_an_implicit_push_from_main(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    for command in (
        "git push",
        "git push origin",
        "git push origin HEAD",
        "git push 2>&1",
        "git push -u origin HEAD 2>&1",
        "git push -u origin HEAD # publish the branch",
        "git push origin HEAD >`git rev-parse --show-toplevel`/push.log",
        'git push origin "$(git branch --show-current)"',
        "git push origin HEAD:$(git branch --show-current)",
        "git push origin HEAD fix/x",
    ):
        assert any(guard.problem(w, str(tmp_path)) for w in guard.segments(command)), command
    assert guard.problem(guard.segments(f"git -C {tmp_path} push")[0], "/")
    new_branch = "git push origin HEAD:fix/$(date +%Y%m%d)"
    assert not any(guard.problem(w, str(tmp_path)) for w in guard.segments(new_branch))
    subprocess.run(["git", "-C", str(tmp_path), "switch", "-q", "-c", "fix/x"], check=True)
    assert guard.problem(guard.segments("git push")[0], str(tmp_path)) is None


def test_guard_follows_cd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import subprocess

    main, feature = tmp_path / "main", tmp_path / "feature"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "fix/x", str(feature)], check=True)
    for command, code in (
        (f"cd {main} && git push origin HEAD", 2),
        ("git push", 0),
        ('cd "$(git rev-parse --show-toplevel)" && git push origin HEAD', 0),
    ):
        event = {"tool_input": {"command": command}, "cwd": str(feature)}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
        assert guard.main() == code, command
    assert "Never push to main" in capsys.readouterr().err


def _temp_repo(monkeypatch: pytest.MonkeyPatch, path: Path):
    """A repo with one commit on main, tracked as origin/main, and verify pointed at it."""
    import subprocess

    for role in ("AUTHOR", "COMMITTER"):  # CI runners have no git identity
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "chore: base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(verify, "ROOT", path)
    return git


def test_pre_push_checks_every_pushed_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git = _temp_repo(monkeypatch, tmp_path)
    head = git("rev-parse", "HEAD")
    unsigned = git("commit-tree", "HEAD^{tree}", "-p", head, "-m", "fix: unsigned")
    zero = "0" * 40
    good = f"refs/heads/a {head} refs/heads/a {head}"
    bad = f"refs/heads/b {unsigned} refs/heads/b {head}"
    deletion = f"(delete) {zero} refs/heads/c {head}"
    new_branch = f"refs/heads/d {unsigned} refs/heads/d {zero}"
    assert verify.cmd_pre_push("origin", [good, deletion]) == 0
    assert verify.cmd_pre_push("origin", [good, bad]) == 1
    assert verify.cmd_pre_push("origin", [new_branch]) == 1
    assert "fix: unsigned is not signed" in capsys.readouterr().err


def test_pre_push_checks_a_recreated_branch_with_a_stale_tracking_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git = _temp_repo(monkeypatch, tmp_path)
    unsigned = git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "fix: unsigned")
    # The branch was deleted on GitHub but this clone still tracks its old tip.
    git("update-ref", "refs/remotes/origin/fix-x", unsigned)
    push = f"refs/heads/fix-x {unsigned} refs/heads/fix-x {'0' * 40}"
    assert verify.cmd_pre_push("origin", [push]) == 1
    assert "fix: unsigned is not signed" in capsys.readouterr().err


def test_pre_push_needs_upstream_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    assert verify.cmd_pre_push("origin", [f"refs/heads/x {'a' * 40} refs/heads/x {'0' * 40}"]) == 1
    assert "git fetch origin main" in capsys.readouterr().err
    # Deleting a branch, or a push with nothing to send, needs no main.
    assert verify.cmd_pre_push("origin", [f"(delete) {'0' * 40} refs/heads/x {'a' * 40}"]) == 0
    assert verify.cmd_pre_push("origin", []) == 0


def test_install_hooks_stays_out_of_a_shared_hooks_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    repo, shared = tmp_path / "repo", tmp_path / "shared-hooks"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    monkeypatch.setattr(verify, "ROOT", repo)
    subprocess.run(["git", "config", "--global", "core.hooksPath", str(shared)], check=True)
    assert verify.cmd_install_hooks() == 1
    assert "core.hooksPath is set" in capsys.readouterr().out
    assert not (shared / "pre-push").exists()
    subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"], check=True)
    assert verify.cmd_install_hooks() == 0
    assert (repo / ".git" / "hooks" / "pre-push").exists()
