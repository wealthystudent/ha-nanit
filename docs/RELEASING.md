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
  label, but never lower than a beta already in flight: a patch merged while
  `v1.13.0-beta.2` exists becomes `v1.13.0-beta.3`.
- **Stable is a promotion.** It tags the exact commit a beta was built from,
  so what ships is what was tested.
- **Nothing half-published.** The GitHub release is created last, after PyPI.
  HACS never sees a release whose zip or `aionanit` version is missing.

## Who can do what

| Action | Who | Enforced by |
|--------|-----|-------------|
| Merge PRs, publish betas (via labels), retry pipelines | Maintainers | Repository role |
| Create stable tags `vX.Y.Z` | Owner | Tag ruleset (beta tags are excluded) |
| Publish a stable release to PyPI and HACS | Owner | `release-stable` environment, owner is the required reviewer |
| Publish to PyPI at all | Only `release.yaml` on `main` | PyPI trusted publishing + `pypi` environment limited to `main` |

The release workflow also refuses tags that are not `vX.Y.Z` or
`vX.Y.Z-beta.N`, and tags that don't point into `main`'s history.

## Release notes

Every PR describes its user-facing change under `## Changelog` in its
description (the PR template has the section). The `PR` workflow requires it
for `feat`/`fix`/`perf` PRs and for any PR with a release label; `none` opts
out when users won't notice the change.

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

## One-time repository setup (owner)

- Environments: `release-stable` with the owner as required reviewer.
  `pypi` limited to the `main` branch.
- Tag ruleset: restrict creation of `refs/tags/v*`, excluding
  `refs/tags/v*-beta.*`, with an admin bypass.
- Branch ruleset on `main`: required checks `CI OK` and `PR Metadata`.
- PyPI trusted publisher for `aionanit`: workflow `release.yaml`, environment
  `pypi`.
