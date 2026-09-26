# Contributing to ha-nanit

Thanks for your interest in contributing! This is the development workflow for
everyone, people and AI agents alike. Code standards, architecture and
invariants are in [AGENTS.md](AGENTS.md). Releases are in
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
just setup   # uv sync into .venv + pre-commit hooks
just check   # everything CI runs on Python code
```

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
just fix           # Auto-fix lint and formatting
just card          # Rebuild the Lovelace card after editing frontend/src (commit the bundle)
just card watch    # Rebuild on every change

just dev           # Dev Home Assistant → http://localhost:8123
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
2. Make your changes. Follow existing code patterns and [AGENTS.md](AGENTS.md).
3. Run `just check`.
4. **Verify in the dev HA instance** (`just dev`) for anything users will notice. This is how the maintainers test your PR too.
5. Open a **pull request** against `main` and fill in the template:
   - **Title**: conventional commit, e.g. `fix: handle token refresh during reconnect`. It becomes the commit on `main`.
   - **Changelog**: what users will notice, in plain sentences. It becomes the release notes. Write `none` if users won't notice (refactors, tests, CI).
6. If the change should ship, it needs a label: `release:patch` (fixes), `release:minor` (features) or `release:major` (breaking). Maintainers add it themselves; from a fork, suggest one in the description and a maintainer adds it. Merging a labelled PR publishes a beta automatically.
7. CI must pass: `CI OK` (lint, types, tests, drift checks, hassfest) and `PR Metadata` (title and changelog). Fix in the same branch and push.
8. A maintainer reviews and squash-merges. The branch is deleted automatically.

`just release` → Create PR does steps 5 and 6 interactively.

### Stacked PRs

A PR can target another PR's branch when it builds on it. CI runs on it all
the same. When the parent is squash-merged, `main` gets a new commit the child
doesn't contain, so move the child over before merging it:

```bash
git fetch origin
git rebase --onto origin/main <parent's last head sha> <child branch>
git push --force-with-lease=<child branch>:<child sha you last saw>
```

Then retarget the child to `main` and, after it merges, check that
`git diff --stat origin/main <child branch>` lists only files `main` changed
on its own. A stacked PR merged into its parent's branch instead of `main`
never reaches `main`.

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

One logical change per commit. If behavior changes, update `README.md` in the
same PR. Pre-commit runs ruff lint and format on every commit; don't bypass it
with `--no-verify`.

### Signed commits

`main` only accepts signed commits. PRs are squash-merged and GitHub signs the
squash commit, so **your PR commits don't need to be signed**. Signing them is
still encouraged, and the owner signs stable release tags. GitHub accepts GPG
or SSH signatures
([docs](https://docs.github.com/en/authentication/managing-commit-signature-verification));
SSH is the quickest to set up:

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

Agents use whatever signing the host has configured. A hardware key that needs
a touch (e.g. a YubiKey) makes every agent commit a deliberate human action.

## Editor setup (optional)

The repo is editor-neutral. It ships an `.editorconfig` and a `[tool.pyright]`
section that points language servers at `.venv`. mypy in `just check` is the
type-checking authority; the editor only gives fast feedback.

- **Neovim**: enable `basedpyright` (or `pyright`) and `ruff` language servers
  (e.g. via mason + nvim-lspconfig), and format Python with `ruff_format` in
  conform.nvim. They pick up `pyproject.toml` and `.venv` from the repo root.
- **VS Code**: the Python, Pylance and Ruff extensions, with `.venv` as the
  interpreter.

## AI assistants

- [AGENTS.md](AGENTS.md) is the tool-neutral brief every agent should read.
- **Claude Code** applies `.claude/settings.json` to anyone using it in this
  repo. Its rules catch the usual forms of releases, tag creation, workflow
  dispatch, force pushes, `--admin` merges and `--no-verify` (accident guards
  that match command text, not a security boundary), ask before pushes and
  merges, and format edited Python with the locked ruff. The format hook
  installs only ruff, so it stays light on a fresh clone. The `review-prs`
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
