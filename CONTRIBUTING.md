# Contributing to ha-nanit

Thanks for your interest in contributing! This is the development workflow for
everyone, people and AI agents alike. Code standards, architecture and
invariants are in [AGENTS.md](AGENTS.md). How every change is verified is in
[docs/VERIFYING.md](docs/VERIFYING.md). Releases are in
[docs/RELEASING.md](docs/RELEASING.md).

## Setup

Prerequisites:

- [uv](https://docs.astral.sh/uv/) (installs the right Python and all dependencies)
- [just](https://just.systems/) (task runner)
- Docker (optional, for the dev Home Assistant instance)
- Node.js 24 (optional, only to rebuild the Lovelace card; `frontend/.nvmrc`)
- A Nanit account (for testing against real hardware)

```bash
git clone https://github.com/wealthystudent/ha-nanit.git
cd ha-nanit
just setup   # uv sync into .venv + the git hooks
just verify  # what CI runs, plus your commits and PR
```

Set up [commit signing](#signed-commits) before your first commit: `main`
only accepts PRs whose commits are all signed.

`uv.lock` pins every dev dependency with hashes, so your environment, the
pre-commit hooks and CI all run the same versions. `uv run` (and every `just`
recipe) re-syncs `.venv` when the lock changes, so there is no separate
install step to forget.

uv uses its own managed Python (`python-preference = "only-managed"`), so a
system Python never leaks in. If `uv sync` fails to replace `.venv` with
"Directory not empty" on macOS, a Finder window on the repo is recreating
`.DS_Store` files: close it, `rm -rf .venv`, and sync again.

## Development loop

```bash
just test          # Integration tests with coverage
just test lib      # aionanit library tests
just test all      # Both
just check         # Lint, format, types and both test suites
just verify        # Before a PR: all of CI, your commits and PR, and the receipt
just fix           # Auto-fix lint and formatting
just card          # Rebuild the Lovelace card after editing frontend/src (commit the bundle)
just card watch    # Rebuild on every change

just dev           # Dev Home Assistant → http://localhost:8123
just dev fix/foo   # Serve the checkout on this branch, or a worktree by folder name
just dev restart   # Restart after code changes
just dev logs      # Follow logs
just dev reset     # Wipe dev state
```

The dev instance mounts `custom_components/` read-only and installs
`packages/aionanit` in editable mode. See [tests/README.md](tests/README.md)
for test details and manual test guides.

`just login` saves a Nanit session to `.nanit-session` (gitignored, owner-only)
for the probing tools (`just events`, `just probe`, `just network`,
`just sound`).

## Making changes

1. **Branch** from `main`: `feat/`, `fix/`, `refactor/`, `docs/`, `test/` or `chore/` + a short description.
2. Make your changes. Follow existing code patterns and [AGENTS.md](AGENTS.md). Add tests that fail without the change.
3. Commit (signed, conventional message). If behavior changes, update `README.md` in the same PR.
4. Run **`just verify`** and fix what it reports. It ends with a receipt and the live test this change needs.
5. **Test it live** as `just verify` lists, in `just dev` or your own Home Assistant. See [docs/VERIFYING.md](docs/VERIFYING.md#the-live-test).
6. Open a **pull request** against `main` and fill in every section of the template:
   - **Title**: conventional commit, e.g. `fix: handle token refresh during reconnect`. It becomes the commit on `main`.
   - **Changelog**: what users will notice, in plain sentences. It becomes the release notes. Write `none` if users won't notice (refactors, tests, CI).
   - **How I tested this**: your live test under `Live test:`, then the `just verify` receipt, unchanged.
   - **AI use**: `none`, or the tool and how you used it.
7. If the change should ship, it needs a label: `release:patch` (fixes), `release:minor` (features) or `release:major` (breaking). Maintainers add it themselves; from a fork, suggest one in the description and a maintainer adds it. Merging a labelled PR publishes a beta automatically.
8. CI must pass: `CI OK` (lint, types, tests, drift checks, hassfest) and `PR Metadata` (title, description and signed commits). Fix in the same branch, push, and run `just verify` again before you ask for review.
9. A maintainer reviews and squash-merges. The branch is deleted automatically.

`just release` → Create PR does steps 6 and 7 interactively.

### Stacked PRs

A PR can target another PR's branch when it builds on it. CI runs on it all
the same. When the parent is squash-merged, `main` gets a new commit the child
doesn't contain, so move the child over before merging it:

```bash
git fetch origin
git rebase --onto origin/main <parent's last head sha> <child branch>
git push --force-with-lease=<child branch>:<child sha you last saw>
```

Don't use `gh pr update-branch` (or merge `main` in) for a formerly stacked
PR: a three-way merge against the parent's squash can silently bring back
lines the child removed, with no conflict to warn you.

Then retarget the child to `main` and, after it merges, check that
`git diff --stat origin/main <head sha>` lists only files `main` changed on
its own. Use the PR's head SHA (`gh pr view <n> --json headRefOid`): the
branch itself is deleted on merge. A stacked PR merged into its parent's
branch instead of `main` never reaches `main`.

### Commit messages

[Conventional commits](https://www.conventionalcommits.org/), imperative,
lowercase, no period:

```
feat: add night vision toggle
fix: handle token refresh during reconnect
refactor: extract protobuf parsing into separate module
docs: update camera IP configuration instructions
test: add coverage for MFA config flow
chore: update locked dev dependencies
```

One logical change per commit. The commit-msg hook checks the subject and
rejects AI co-author trailers; pre-commit runs ruff lint and format. Don't
bypass them with `--no-verify`.

### Signed commits

`main` only accepts signed commits, and that applies to the commits in a PR
too. **An unsigned commit, or one GitHub can't verify, blocks the merge.** The
owner also signs stable release tags. GitHub accepts GPG or SSH signatures
([docs](https://docs.github.com/en/authentication/managing-commit-signature-verification));
SSH is the quickest to set up:

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

Add the same public key to your GitHub account as a signing key, or the commits
show up as unverified. The commit email must be a verified email on that
account too.

The pre-push hook stops an unsigned commit before it leaves your machine,
`just verify branch` checks the whole branch, and `PR Metadata` names any
commit GitHub can't verify, and why.

If a branch you already pushed has unsigned commits, sign them in place (same
base, same changes) and force-push. From a fork, use your remote for this repo
(often `upstream`) instead of `origin`:

```bash
git fetch origin main
git rebase --force-rebase --gpg-sign "$(git merge-base HEAD origin/main)"
git push --force-with-lease
```

The force-push needs a fresh approval before the PR can merge.

If the branch has merge commits, don't rebase: the rebase drops them, and
with them any change made in a merge, without stopping. Squash the branch into
one signed commit instead with
`git reset --soft "$(git merge-base HEAD origin/main)"` and `git commit -S`.

Agents use whatever signing the host has configured, and never change it. On a
host without signing, an agent stops and asks its person to set it up rather
than pushing unsigned commits. A hardware key that needs a touch (e.g. a
YubiKey) makes every agent commit a deliberate human action.

## Editor setup (optional)

The repo is editor-neutral. It ships an `.editorconfig` and a `[tool.pyright]`
section that points language servers at `.venv`. mypy in `just check` is the
type-checking authority; the editor only gives fast feedback.

- **Neovim**: enable `basedpyright` (or `pyright`) and `ruff` language servers
  (e.g. via mason + nvim-lspconfig), and format Python with `ruff_format` in
  conform.nvim. They pick up `pyproject.toml` and `.venv` from the repo root.
- **VS Code**: the Python, Pylance and Ruff extensions, with `.venv` as the
  interpreter.

## AI tools

AI tools are welcome, as tools. The rules follow the
[Open Home Foundation AI policy](https://contributors.home-assistant.io/start/ai-policy/)
that Home Assistant itself uses:

- **A person is behind every PR.** You direct the work, read every line, can
  explain it, and answer review questions yourself. Agents that open PRs on
  their own, with no person reviewing their work, are not accepted.
- **Say how you used AI** under `## AI use` in the PR: `none`, `assisted`, or
  `generated and reviewed`, with the tool.
- **No AI credit in commits.** No `Co-authored-by` trailers for AI tools and
  no "generated with" lines. GitHub copies them into the squash commit on
  `main`. The commit-msg hook and `PR Metadata` reject them.
- **Verify like everyone else.** An agent runs `just verify` and pastes its
  receipt. It never claims a test it didn't run; without the hardware it
  writes `not tested live` and why.

Setup for agents:

- [AGENTS.md](AGENTS.md) is the tool-neutral brief. Codex, Copilot, Cursor and
  Claude Code read it; `CLAUDE.md` imports it so Claude Code always does.
- **Claude Code** applies `.claude/settings.json` to anyone using it in this
  repo. It turns off Claude's own commit and PR credit, allows `just verify`,
  and denies the usual forms of releases, tag creation, workflow dispatch,
  `--admin` merges and `--no-verify`. A hook blocks a bare `--force` push,
  pushes to `main` and changes to signing, while allowing the
  `--force-with-lease` that re-signing your own branch needs. These match
  command text, so they are accident guards, not a security boundary.
  Another hook formats edited Python with the locked ruff. The `review-prs`
  skill reviews open PRs. Personal settings go in
  `.claude/settings.local.json` (gitignored).
- Keep personal tool config (MCP server URLs, tokens) out of the repo: use
  your user-level config or `.git/info/exclude`.
- Point agents that talk to Home Assistant at the dev instance, not your home.

## Security

All contributions must pass security review. Key rules:

- Never log credentials, tokens, or stream URLs containing tokens.
- Validate user input with voluptuous schemas in config/options flows.
- Sanitize data from external APIs before using it as entity names.
- No `eval()`, `exec()`, `os.system()`, or `subprocess(shell=True)`.
- Secrets in `entry.data` only, never in `entry.options`.
- Use `async_redact_data()` in diagnostics.

Full checklist: [`docs/SECURITY_AUDIT_CHECKLIST.md`](docs/SECURITY_AUDIT_CHECKLIST.md).

## Reporting issues

- **Bugs**: [Bug report template](https://github.com/wealthystudent/ha-nanit/issues/new?template=bug_report.yml)
- **Features**: [Feature request template](https://github.com/wealthystudent/ha-nanit/issues/new?template=feature_request.yml)

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
