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
- Checks per PR: `gh pr checks <n>`
- Mergeability: `gh pr view <n> --json mergeable,mergeStateStatus,reviewDecision`
- Author history: `gh pr list --author <login> --state merged --limit 20 --json number,title`
- Open issues the PRs reference: `gh issue view <n>`
- `docs/SECURITY_AUDIT_CHECKLIST.md`: use its file-to-section map to scope each PR.

## 2. Review each PR

| Dimension | Look for | Rating |
|-----------|----------|--------|
| Value | Real bug users hit, security gap closed, maintainability gained, or churn | HIGH / MEDIUM / LOW / NEGATIVE |
| Security | Applicable checklist items. Always: no tokens or token-bearing URLs in logs, TLS verified for cloud, malformed protobuf handled, API names sanitized, secrets only in `entry.data`, no `eval`/`exec`/`shell=True` | PASS / CONCERN [sev] / FAIL [sev] |
| Quality | No `# type: ignore` or `cast()`/`Any` escapes, no blocking I/O in async paths, `(TimeoutError, aiohttp.ClientError)` caught together, tests that would fail without the fix, follows neighboring code | EXCELLENT / GOOD / NEEDS WORK / REJECT |
| Invariants | S&L changes respect every rule in AGENTS.md "aionanit_sl Invariants"; unique IDs unchanged or migrated | OK / VIOLATION |
| Metadata | Conventional title; `## Changelog` written for users (it becomes the release notes); release label matches the change size | OK / FIX |
| Contributor | First-time vs established, domain understanding, description explains what and why | TRUSTED / CREDIBLE / UNKNOWN / CAUTION |

The `PR Metadata` check validates title and changelog presence. Judge the
changelog's content: user-facing wording, accurate, no internal jargon.

## 3. Report and wait

Present a verdict matrix, then 2-3 sentences per PR (what it does, concerns,
recommendation: MERGE / NEEDS CHANGES / CLOSE):

```
| PR | Title | Value | Security | Quality | Metadata | Verdict |
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
   force-push to someone else's branch.
3. Wait for checks: `gh pr checks <n> --watch --fail-fast`. All required
   checks (`CI OK`, `PR Metadata`) must pass. Never merge with `--admin`.
4. Merge: `gh pr merge <n> --squash --delete-branch`. The PR title and body
   become the commit on main. Afterwards, `git diff --stat origin/main
   <merged branch>` should list only files `main` changed on its own.

A merged PR with a `release:*` label publishes a beta automatically. Verify:
`gh run list --workflow auto-beta.yaml --limit 3` then
`gh run list --workflow release.yaml --limit 3`.

## 5. Stable releases

Stable releases are the owner's call and run only through `just release`
(interactive, signs the tag, approves the gate). Summarize which beta is ready
(`gh release list --limit 5`, and `just notes` for what it contains) and hand
over. Do not create tags, releases or dispatch workflows yourself.
