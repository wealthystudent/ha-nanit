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
    errors, _ = verify.pr_body_problems("chore: x", template, [], "someone", [HEAD])
    text = " ".join(errors)
    assert "'## AI use' is missing or empty" in text
    assert "'Live test:'" in text
    assert "no `just verify` receipt" in text


def test_live_test_must_say_something() -> None:
    errors, _ = problems(body(live=""))
    assert any("'Live test:'" in e for e in errors)
    assert problems(body(live="not tested live: no camera here"))[0] == []


def test_live_test_may_continue_on_the_next_line() -> None:
    assert problems(body(live="\n\nIn my own HA 2026.11.1 the stream came back."))[0] == []


def test_receipt_from_another_branch_fails() -> None:
    errors, _ = problems(body(), shas=["c" * 40])
    assert any("not in this PR" in e for e in errors)


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
    assert any("no `just verify` receipt" in e for e in problems(unfenced)[0])
    unclosed = body().replace("result: pass\n```", "result: pass")
    assert any("no `just verify` receipt" in e for e in problems(unclosed)[0])


def test_longer_fences_work() -> None:
    text = body().replace("```text", "````text").replace("result: pass\n```", "result: pass\n````")
    assert problems(text) == ([], [])
    hidden = body(live="").replace(RECEIPT, "````\nLive test: fake\n```\n````\n" + RECEIPT)
    assert any("'Live test:'" in e for e in problems(hidden)[0])


def test_local_run_only_warns_about_an_outdated_receipt() -> None:
    errors, warnings = verify.pr_body_problems(
        "fix: x", body(), [], "someone", ["c" * 40], receipt_required=False
    )
    assert errors == []
    assert any("not in this PR" in w for w in warnings)


def test_bots_skip_the_written_sections() -> None:
    assert verify.pr_body_problems("chore(deps): bump x", "", [], "dependabot[bot]", []) == (
        [],
        [],
    )


def test_title_and_changelog_rules_still_apply() -> None:
    errors, _ = verify.pr_body_problems("Fix stuff", body(), [], "someone", [HEAD])
    assert any("conventional commit" in e for e in errors)


# ─── Commits ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
        "co-authored-by: codex <codex@openai.com>",
        "Co-authored-by: Cursor Agent <cursoragent@cursor.com>",
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
        "fix: x\n\nCo-authored-by: Devin Smith <devin@example.com>",
        "fix: x\n\nCo-authored-by: Jules Martin <jules@example.org>",
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
) -> int:
    def fake_api(url: str, token: str) -> list:
        return commits if "/commits" in url else [{"filename": "custom_components/nanit/sensor.py"}]

    monkeypatch.setattr(verify, "_api", fake_api)
    for key, value in {
        "GITHUB_REPOSITORY": "wealthystudent/ha-nanit",
        "PR_NUMBER": "1",
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
        ("git config --unset commit.gpgsign", True),
        ("git config set commit.gpgsign false", True),
        ("git config unset user.signingkey", True),
        ("git config --file .git/config commit.gpgsign false", True),
        ("git config -f x.cfg --get commit.gpgsign", False),
        ("git push --force", True),
        ("git push -uf origin fix/x", True),
        ("git push origin +fix/x", True),
        ("git push origin HEAD:main", True),
        ("git -c commit.gpgsign=false commit -m 'fix: x'", True),
        ("git config commit.gpgsign false", True),
        ("cd x && git commit -n -m 'fix: x'", True),
        ("git commit --no-gpg-sign -m 'fix: x'", True),
    ],
)
def test_guard(command: str, blocked: bool) -> None:
    reasons = [guard.problem(words) for words in guard.segments(command)]
    assert any(reasons) is blocked


def test_guard_blocks_an_implicit_push_from_main(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    for command in ("git push", "git push origin", "git push origin HEAD"):
        words = guard.segments(command)[0]
        assert guard.problem(words, str(tmp_path)), command
    assert guard.problem(guard.segments(f"git -C {tmp_path} push")[0], "/")
    subprocess.run(["git", "-C", str(tmp_path), "switch", "-q", "-c", "fix/x"], check=True)
    assert guard.problem(guard.segments("git push")[0], str(tmp_path)) is None
