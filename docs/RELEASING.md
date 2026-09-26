# Releasing

The git tag is the only source of truth for versions. Version files on `main`
hold a `0.0.0` placeholder, and the release workflow writes the real version
into the build. Nothing is committed to `main` at release time.

## Flow

```
PR with release:patch|minor|major label
  │  merge (squash)
  ▼
auto-beta.yaml ── tags the merge commit vX.Y.Z-beta.N ── dispatches ──┐
                                                                     ▼
release.yaml:  resolve → CI gate → build → PyPI → GitHub pre-release + nanit.zip
                                                                     │
HACS beta users get it ◄─────────────────────────────────────────────┘

owner: just release → Release stable
  │  signed tag vX.Y.Z on the beta's commit, dispatch, approve the gate
  ▼
release.yaml:  resolve → approve (release-stable) → CI gate → build → PyPI → GitHub release
```

- **Betas are automatic.** Merging a PR with a `release:*` label publishes a
  beta. PRs without a label (docs, tests, chores) ship with the next labelled
  merge or stable.
- **One beta train.** The next version is the latest stable bumped by the
  largest label among all PRs merged since it, and never lower than a beta
  already in flight: a patch merged while `v1.13.0-beta.2` exists becomes
  `v1.13.0-beta.3`. Because the bump looks at every PR since the stable, an
  auto-beta run cancelled by a burst of merges loses nothing; the next run's
  beta contains that PR and honours its label.
- **Stable is a promotion.** It tags the exact commit of a published beta, so
  what ships is what was tested. `just release` only offers betas that have a
  GitHub pre-release; a bare beta tag was never gated.
- **Nothing half-published.** The GitHub release is created last, after PyPI.
  HACS never sees a release whose zip or `aionanit` version is missing.

## Who can do what

| Action | Who | Enforced by |
|--------|-----|-------------|
| Merge PRs, publish betas (via labels), retry pipelines | Maintainers | Repository role |
| Create stable tags `vX.Y.Z` | Owner | Tag ruleset (beta tags are excluded) |
| Publish a stable release to PyPI and HACS | Owner | `release-stable` environment, owner is the required reviewer |
| Publish to PyPI at all | Only `release.yaml` on `main` | PyPI trusted publishing + `pypi` environment limited to `main` |

The release workflow also refuses:

- tags that are not `vX.Y.Z` or `vX.Y.Z-beta.N`,
- tags that don't point into `main`'s history,
- tags whose tree has no `uv.lock` (built before this pipeline, see Retrying),
- stable tags while `release-stable` has no required reviewer. GitHub creates
  a missing environment with no protection, so the workflow fails closed
  instead of trusting it.

## Release notes

Every PR describes its user-facing change under `## Changelog` in its
description (the PR template has the section). The `PR Metadata` check
enforces it:

- `feat`/`fix`/`perf` PRs need user-facing text or a literal `none`. The
  untouched template (an empty section) fails.
- PRs with a release label need real text; `none` isn't enough for something
  that publishes a release.
- Other types may leave it empty.

`tools/release_notes.py` assembles notes from every PR merged since the
previous stable release, grouped into Breaking changes, Added, Fixed and
Changed, and credits the authors. Betas therefore show everything the next
stable will contain. Preview what `main` would ship with `just notes`.

To fix notes after release, edit the PR's Changelog section and re-run the
pipeline for that tag (`just release` → Retry). The rerun is idempotent: PyPI
skips existing files, the zip and notes are replaced.

`CHANGELOG.md` holds the history up to 1.12.2.

## Commands

```bash
just notes      # preview release notes for what main would ship now
just release    # create PR, label, merge, release stable (owner), retry
```

## Retrying a failed pipeline

Fix the workflow on `main` (via PR), then `just release` → Retry and pick the
tag. `release.yaml` always runs from `main`, so the fix applies while the build
still uses the original tag's code.

Because the gate is `main`'s `ci.yaml` run against the tag's tree, only tags
built on this pipeline (their tree has `uv.lock`) can be retried. Retry hides
older tags and the workflow refuses them.

## Repository settings (owner)

The guarantees above depend on these settings. Only admins can change them.

| Setting | Value | Why |
|---------|-------|-----|
| Merge methods | Squash only, title = PR title, body = PR body | Commits on `main` are GitHub-signed squashes, so contributor commits don't need signing. Release notes find PRs by the `(#N)` squash suffix. |
| Branch ruleset on `main` | Signed commits, PR required, required checks `CI OK` and `PR Metadata` | Nothing reaches `main` unreviewed or untested. |
| Tag ruleset "only wealthy" (exists) | All tags: no deletion, no update, no force push, signed; admin bypass | A published version can never be moved and rebuilt from different code. |
| Tag ruleset for stable tags | Restrict creation of `refs/tags/v*`, excluding `refs/tags/v*-beta.*`; admin bypass | Only the owner can cut a stable tag. Beta tags stay open for auto-beta. |
| Environment `release-stable` | Required reviewer: owner | Owner approves every stable publish (`just release` does it for you). |
| Environment `pypi` | Deployment branches: `main` only | Only `main`'s `release.yaml` can publish. |
| Actions | "Allow GitHub Actions to create and approve pull requests" off | No workflow can approve its own PR. |
| PyPI trusted publisher for `aionanit` | Workflow `release.yaml`, environment `pypi` | No long-lived PyPI token exists. |
| Collaborators | Maintainers get the Maintain role | Merge, label, retry, betas; no settings or stable tags. |
