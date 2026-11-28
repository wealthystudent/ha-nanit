---
name: review-prs
description: Review open ha-nanit PRs with security audit, code quality, value assessment and contributor context, then merge the approved ones. Use when asked to review PRs, merge PRs, check open PRs, or prepare a release from pending work.
---

# Review PRs

Senior-engineer review of open PRs for ha-nanit. Paths are relative to the
repository root. Project rules live in `AGENTS.md`; read the sections its
Context Router points to for the files each PR touches.

## 1. Gather context (in parallel)

- Open PRs: `gh pr list --state open --json number,title,author,headRefName,baseRefName,labels,body,additions,deletions,changedFiles,url`
- Diff per PR: `gh pr diff <n>`
- Checks per PR: `gh pr checks <n>`. A failing `PR Metadata` names what the description or signatures lack; its job summary lists what the live test must show.
- Signatures per PR: `gh api repos/wealthystudent/ha-nanit/pulls/<n>/commits --jq '.[] | [.sha[:8], .commit.verification.verified, .commit.verification.reason] | @tsv'`
- What each touched area needs: `docs/VERIFYING.md` (the area table) and `uv run tools/verify.py areas`
- Mergeability: `gh pr view <n> --json mergeable,mergeStateStatus,reviewDecision`
- Author history: `gh pr list --author <login> --state merged --limit 20 --json number,title`
- Open issues the PRs reference: `gh issue view <n>`
- `docs/SECURITY_AUDIT_CHECKLIST.md`: use its file-to-section map to scope each PR.

Review from the diff and CI. Don't check out or run a PR's code locally
(`gh pr checkout`, `uv sync`, `just check`) when its author has no merged PRs
here: its tests, conftest and build hooks would run as the owner, with their
GitHub token and Home Assistant in reach. CI already runs the full suite.

## 2. Review each PR

| Dimension | Look for | Rating |
|-----------|----------|--------|
| Value | Real bug users hit, security gap closed, maintainability gained, or churn | HIGH / MEDIUM / LOW / NEGATIVE |
| Security | Applicable checklist items. Always: no tokens or token-bearing URLs in logs, TLS verified for cloud, malformed protobuf handled, API names sanitized, secrets only in `entry.data`, no `eval`/`exec`/`shell=True` | PASS / CONCERN [sev] / FAIL [sev] |
| Quality | No `# type: ignore` or `cast()`/`Any` escapes, no blocking I/O in async paths, `(TimeoutError, aiohttp.ClientError)` caught together, tests that would fail without the fix, follows neighboring code | EXCELLENT / GOOD / NEEDS WORK / REJECT |
| Invariants | S&L changes respect every rule in AGENTS.md "aionanit_sl Invariants"; unique IDs unchanged or migrated | OK / VIOLATION |
| Verification | The live test covers every area the PR touches (docs/VERIFYING.md), on hardware that could run it; `not tested live` says why and is flagged for a maintainer test; the receipt is for the head commit, or a later commit only changed what CI covers | PROVEN / PARTIAL / UNTESTED |
| Metadata | Every template section filled in for real, not just enough to pass; conventional title; `## Changelog` written for users (it becomes the release notes); `## AI use` present; release label matches the change size; every commit verified, no AI co-author trailers | OK / FIX |
| Contributor | First-time vs established, domain understanding, description explains what and why | TRUSTED / CREDIBLE / UNKNOWN / CAUTION |

The `PR Metadata` check validates the title, the sections and the receipt,
and that every commit is verified. It can't judge content: judge the
changelog (user-facing wording, accurate, no internal jargon) and the live
test (does it show what the areas need, or only that something ran). A PR
that ignores the template, or reads like unreviewed AI output its author
can't explain, gets NEEDS CHANGES with what is missing, as CONTRIBUTING.md's
AI tools section says.

## 3. Report and wait

Present a verdict matrix, then 2-3 sentences per PR (what it does, concerns,
recommendation: MERGE / NEEDS CHANGES / CLOSE):

```
| PR | Title | Value | Security | Quality | Verification | Metadata | Verdict |
```

Ask which to merge. **Do not merge, comment, label or approve without explicit
approval per PR.** Offer to post requested changes as a review comment.

## 4. Merge approved PRs

Order: smallest and foundational first, broad docs last. For each:

1. Check the base: `gh pr view <n> --json baseRefName`. Only merge PRs whose
   base is `main`. A stacked PR merged into its parent's branch never reaches
   `main`; wait for the parent, then move the child over (CONTRIBUTING.md,
   "Stacked PRs") and ask the author or owner to retarget it.
2. If behind main or conflicting: `gh pr update-branch <n>` (merges main into
   the PR server side). If that conflicts, ask the author to rebase. Never
   force-push to someone else's branch. Exception: a PR that used to be
   stacked must not be updated this way, since merging main in can silently
   restore lines it removed. It needs the rebase in CONTRIBUTING.md.
3. Wait for checks: `gh pr checks <n> --watch --fail-fast`. All required
   checks (`CI OK`, `PR Metadata`) must pass, which includes every commit
   being signed. Never merge with `--admin`, and never ask an author to drop
   signing: point them to CONTRIBUTING.md "Signed commits".
4. Note the head SHA (`gh pr view <n> --json headRefOid`), then merge:
   `gh pr merge <n> --squash --delete-branch`. The PR title and body become
   the commit on main. Afterwards, `git diff --stat origin/main <head sha>`
   should list only files `main` changed on its own.

A merged PR with a `release:*` label publishes a beta automatically. Verify:
`gh run list --workflow auto-beta.yaml --limit 3` then
`gh run list --workflow release.yaml --limit 3`.

## 5. Stable releases

Stable releases are the owner's call and run only through `just release`
(interactive, signs the tag, approves the gate). Summarize which beta is ready
(`gh release list --limit 5`, and `just notes` for what it contains) and hand
over. Do not create tags, releases or dispatch workflows yourself.
