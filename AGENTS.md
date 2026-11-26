# ha-nanit — AGENTS.md

> For AI agents. Read the section relevant to your task — you don't need to read everything every time.
> Use the [Context Router](#context-router) to find which sections apply.
> The development workflow (setup, branches, PRs, signing, AI use) is in [CONTRIBUTING.md](CONTRIBUTING.md) and applies to agents too.
> Every change is verified as [docs/VERIFYING.md](docs/VERIFYING.md) says: `just verify`, a live test, and the PR description.

## Context Router

| If your task involves…              | Read sections                                      |
|--------------------------------------|----------------------------------------------------|
| Any change                           | [Code Standards](#code-standards), [Workflow](#workflow), [Guardrails](#guardrails), [docs/VERIFYING.md](docs/VERIFYING.md) |
| Integration code (`custom_components/`) | Above + [Architecture](#architecture), [HA Integration Patterns](#ha-integration-patterns) |
| Client library (`packages/aionanit/`)  | Above + [Architecture](#architecture), [aionanit Patterns](#aionanit-patterns) |
| Sound & Light (`aionanit_sl/`)       | Above + [aionanit_sl Invariants](#aionanit_sl-sound--light-invariants) |
| Connection/WebSocket work            | Above + [docs/CONNECTION_RELIABILITY.md](docs/CONNECTION_RELIABILITY.md) |
| Lovelace card (`frontend/`)          | [Code Standards](#code-standards), [Workflow](#workflow) |
| Security review                      | [Security](#security), [docs/SECURITY_AUDIT_CHECKLIST.md](docs/SECURITY_AUDIT_CHECKLIST.md) |
| PR review                            | [Workflow](#workflow), [Security](#security), [Guardrails](#guardrails), [docs/VERIFYING.md](docs/VERIFYING.md#for-reviewers), `.claude/skills/review-prs/` |
| Release                              | [Releases](#releases), [docs/RELEASING.md](docs/RELEASING.md) |

---

## Architecture

Monorepo with two packages for Nanit baby camera Home Assistant integration:

```
custom_components/nanit/   ← HA integration (Python, async)
packages/aionanit/         ← Nanit API client library (published to PyPI)
frontend/                  ← Lovelace card source (TypeScript, Rollup → nanit-card.js)
tests/unit/                ← Integration tests (80% coverage threshold)
dev/                       ← Docker-based dev HA instance
tools/                     ← CLI utilities (login, events, probe, release)
docs/                      ← Security checklist, connection reliability, testing
```

**Data flow:**
- **Push sensors**: Camera → WebSocket → `NanitCamera.subscribe()` → `NanitPushCoordinator` → entities
- **Cloud events**: `NanitCloudCoordinator` polls `GET /babies/{uid}/messages` every 30s
- **Camera stream**: `camera.stream_source()` returns RTMPS URL with fresh access token
- **Commands**: Entity → `NanitCamera` → WebSocket → camera

**Multi-camera**: One config entry per Nanit account (unique_id = email). `NanitHub` auto-discovers all babies/cameras. Entity unique IDs: `{camera_uid}_{key}`.

### Key files

| File | Purpose |
|------|---------|
| `__init__.py` | Entry setup/unload/migrate, `NanitData` dataclass |
| `hub.py` | `NanitHub` lifecycle, `CameraData` per-camera grouping |
| `config_flow.py` | Credentials + MFA, reauth, options (Sound & Light IP, read-only) |
| `coordinator.py` | `NanitPushCoordinator` (WebSocket push) + `NanitCloudCoordinator` (polling) |
| `entity.py` | `NanitEntity` base class with availability logic |
| `camera.py`, `sensor.py`, `binary_sensor.py`, `switch.py`, `number.py` | Entity platforms |
| `manifest.json` | Version, requirements, HA metadata |
| `aionanit_sl/` | Sound & Light transport (see [invariants](#aionanit_sl-sound--light-invariants)) |
| `aionanit/camera.py` | `NanitCamera` state machine, subscribe, commands |
| `aionanit/auth.py` | `TokenManager` (auto-refresh, token change callback) |
| `aionanit/ws/transport.py` | `WsTransport` (WebSocket connection, reconnect, keepalive) |
| `frontend/src/nanit-card.ts` | Lovelace card main component (LitElement) |
| `frontend/src/styles.ts` | All card CSS |
| `custom_components/nanit/frontend/` | Compiled card bundle + auto-registration |

---

## Code Standards

- **Python**: 3.14 (Home Assistant's floor, pinned in `.python-version`). aionanit itself supports 3.12+. Fully async — no blocking I/O in the event loop.
- **Environment**: uv. `uv.lock` pins every dev dependency; run tools through `uv run` or `just`.
- **Linter**: Ruff (rules: B, BLE, C4, D, E, F, I, ICN, N, PGH, PIE, RUF, SIM, T20, UP, W). Line length: 100.
- **Type checking**: mypy strict mode (targets in `pyproject.toml`). All functions must have type hints.
- **Formatting**: Ruff formatter (enforced via pre-commit).
- **Strings**: User-facing text in `strings.json` / `translations/en.json` — no hardcoded English.
- **Imports**: isort via Ruff. Known first-party: `aionanit`, `custom_components.nanit`.
- **Naming**: Follow existing patterns. Never change entity unique IDs or class names without a migration plan.
- **Tests**: New features must include tests. Coverage threshold: 80% (enforced in CI).
- **Card**: edit `frontend/src/`, then `just card` and commit the rebuilt `nanit-card.js` (CI checks it matches).
- **Generated code**: never hand-edit `*_pb2.py` / `*_pb2.pyi`.

### Commands

```bash
just setup            # uv sync into .venv + git hooks (pre-commit, commit-msg, pre-push)
just verify           # Before any PR: all of CI, your commits and PR; prints the receipt
just verify branch    # Commit and PR checks only, in seconds
just check            # Lint + format + typecheck + tests (the quick loop)
just fix              # Auto-fix lint issues and reformat
just test             # Integration tests with coverage (custom_components)
just test lib         # aionanit library tests
just test all         # Both test suites
just card             # Rebuild the Lovelace card bundle
just dev              # Start dev HA instance → http://localhost:8123
just dev restart      # Restart after code changes
just dev <branch>     # Serve the checkout that has <branch>, such as your worktree
just notes            # Preview release notes for what main would ship
```

---

## Workflow

Full details in [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/VERIFYING.md](docs/VERIFYING.md). What agents must get right:

- A person directs your work and answers for the PR. Don't open PRs no one asked for.
- Branch from `main`: `feat/`, `fix/`, `refactor/`, `docs/`, `test/`, `chore/` + short description.
- Commits and PR titles use conventional commits: `<type>: <description>`, imperative, lowercase, no period. The PR title becomes the squash commit on `main`.
- Commits are signed with the host's existing signing setup. Never change or disable it. If the host has no signing, stop and ask your person to set it up; never push unsigned commits.
- No AI co-author trailers or "generated with" lines in commit messages. The commit-msg hook and `PR Metadata` reject them.
- If behavior or user-facing functionality changes, update `README.md` in the same PR.
- Check the change against the applicable sections of [`docs/SECURITY_AUDIT_CHECKLIST.md`](docs/SECURITY_AUDIT_CHECKLIST.md) before opening the PR.

### Verifying

1. Commit everything, then run `just verify`. Fix every `fail` and rerun until it ends with `result: pass`.
2. Do the live test it lists for the areas the change touches. Agents can't run Docker or reach hardware in most sandboxes: then write `not tested live` and why, so a maintainer tests it. Never describe a test you didn't run, and never edit the receipt.
3. Open the PR with every section of `.github/pull_request_template.md` filled in:
   - `## Changelog`: user-facing text that becomes the release notes, or `none`.
   - `## How I tested this`: `Live test:` with what you ran and saw, then the `just verify` receipt, unchanged.
   - `## AI use`: the tool, and `assisted` or `generated and reviewed`.
4. After each push, run `just verify` again and replace the receipt, so it names the PR's head commit. `just verify branch` checks the PR description in seconds.
5. Release labels (`release:patch|minor|major`) publish a beta on merge. Pick the size that matches the change; leave unlabelled if nothing user-facing changed.

### Releases

Full details: [docs/RELEASING.md](docs/RELEASING.md).

- **Betas are automatic**: merging a PR with a `release:*` label tags the merge commit (`vX.Y.Z-beta.N`, one beta train) and publishes it.
- **Stable is owner only**: `just release` → Release stable promotes a beta's commit with a signed `vX.Y.Z` tag. A tag ruleset and the `release-stable` environment enforce this.
- **Versions come from the tag.** `manifest.json` and `packages/aionanit/pyproject.toml` hold a `0.0.0` placeholder on `main`; `release.yaml` injects the real version at build time, and the integration pins `aionanit==<version>`. Never bump version files by hand.
- **Release notes** are assembled from each merged PR's `## Changelog` section. There is no changelog file to edit.
- **Rollback**: forward-fix via a new PR. **Pipeline fix**: fix the workflow on `main`, then the owner retries from `just release`.

---

## Security

**Every PR and release MUST pass security review before merge.**

Full checklist: [`docs/SECURITY_AUDIT_CHECKLIST.md`](docs/SECURITY_AUDIT_CHECKLIST.md) (24 categories, 202 items).

### For AI agents performing reviews

1. Read `docs/SECURITY_AUDIT_CHECKLIST.md`.
2. Use the [File → Section Map](docs/SECURITY_AUDIT_CHECKLIST.md#file-to-section-map) to scope the review.
3. Report each item as **PASS** (with evidence), **FAIL [severity]** (with file:line + fix), or **N/A**.
4. Block merge on any Critical or High failure.

### Key constraints (ha-nanit-specific)

- `ssl.CERT_NONE` for local camera connections is an accepted risk (Nanit self-signed certs). Cloud MUST verify TLS.
- RTMPS stream URLs contain embedded access tokens — never log these.
- Protobuf over WebSocket is the primary untrusted deserialization surface. Handle malformed data gracefully.
- Baby/camera names from Nanit API become HA entity names — sanitize to prevent stored XSS.
- All secrets in `entry.data` only. Diagnostics must use `async_redact_data()`.
- No `eval()`, `exec()`, `os.system()`, or `subprocess(shell=True)`.

---

## Guardrails

### Must do
- Run `just verify` before opening a PR and after every push to it, and paste its receipt.
- Follow existing code patterns — read neighboring files before writing new ones.
- Ask questions before starting work if anything is unclear. Do not guess.
- Do the live test `just verify` lists in the dev Home Assistant instance (`just dev`), not a production one, or say plainly that you didn't.

### Must not
- Suppress type errors (`# type: ignore`, `cast()` to bypass, `Any` as escape hatch).
- Change entity unique IDs or device identifiers without a migration plan.
- Introduce blocking I/O in async code paths.
- Log or store credentials, tokens, or URLs containing tokens.
- Add dependencies without full supply chain review (Section 10 of security checklist).
- Pin aionanit's runtime dependencies (`aiohttp`, `protobuf` in `[project] dependencies`) exactly. They stay broad ranges (e.g. `>=3.9.0,<4`) because exact pins fight Home Assistant's own dependency resolution. Exact pins belong in `uv.lock` only.
- Commit directly to `main` — always use a PR.
- Disable or reconfigure the host's commit signing, or bypass pre-commit hooks with `--no-verify`. (PR commits need to be signed for the PR to merge. See [Signed commits](CONTRIBUTING.md#signed-commits).)
- Merge with `--admin`, or force-push to a branch you did not create. On your own branch, only `--force-with-lease`.
- **Release anything**: never run `just release`, create or edit tags and GitHub releases, or dispatch workflows. Releases are human actions.
- **Edit `AGENTS.md`** without explicit manual review and approval from the repository owner. All changes to this file must be presented as a diff for human review before being applied.
- **Add AI co-author attribution** — never include any AI agent as a co-author or in commit trailers.

### Enforcement

- **Every tool**: the git hooks from `just setup` reject unsigned pushes and AI co-author trailers, and `PR Metadata` in CI checks the title, the description and every commit's signature.
- **Claude Code**: `.claude/settings.json` is checked in and applies to anyone running Claude Code in this repo. `CLAUDE.md` imports this file. The settings turn off Claude's commit and PR credit, deny the common forms of the actions above (releases, tag creation, workflow dispatch and reruns, `--admin` merges, `--no-verify`) and ask before merges and `AGENTS.md` edits. `.claude/hooks/guard-git.py` blocks a bare `--force` push, pushes to `main` and changes to signing, and allows `--force-with-lease`. They match command text, so they are accident guards, not a security boundary: an unusual spelling slips past, and the rules above still apply as written. A hook formats edited Python files with the locked ruff. Personal overrides go in `.claude/settings.local.json` (gitignored).
- **Other agents**: follow the rules as written.

---

## HA Integration Patterns

Follow [Home Assistant developer docs](https://developers.home-assistant.io/) (latest version). Minimum HA: **2025.12+**.

- **Config flow**: Credentials → MFA → reauth support. Options flow for Sound & Light IPs and read-only mode.
- **Runtime data**: Use `ConfigEntry.runtime_data` (typed as `NanitData`) — not `hass.data`.
- **Push coordinator**: `DataUpdateCoordinator.async_set_updated_data()` for WebSocket push data. Do NOT poll.
- **Polling coordinator**: `DataUpdateCoordinator` with `update_interval` for cloud events.
- **Entity base**: Subclass `NanitEntity` (in `entity.py`). Availability is managed by the coordinator.
- **Platform setup**: Iterate `entry.runtime_data.cameras.values()` to create entities for all cameras.
- **Config entry**: Version 2 with migration from v1. `unique_id` = account email.

## aionanit Patterns

- All I/O: async (`aiohttp`, `asyncio`). Use the shared `aiohttp.ClientSession` — do not create your own.
- **aiohttp timeout caveat**: Always catch `(TimeoutError, aiohttp.ClientError)` together. aiohttp's `total` timeout raises the builtin `TimeoutError`, not a `ClientError` subclass — catching only `ClientError` lets timeouts escape and misclassify (see PR #112).
- Protobuf: generated from `proto/nanit.proto` via `scripts/generate_proto.py`.
- WebSocket keepalive: ping every 25s, read deadline 60s.
- Token lifetime: 3600s. Pre-emptive refresh at ~3300s (5 min before expiry).
- Local connections: self-signed TLS (`ssl.CERT_NONE`). Max 1 WebSocket per camera.
- Background tasks: follow `_start_*` / `_cancel_*` pattern, wire into `async_start()` / `async_stop()` / `_async_reconnect()`.
- Connection reliability details: [docs/CONNECTION_RELIABILITY.md](docs/CONNECTION_RELIABILITY.md).

## aionanit_sl (Sound & Light) Invariants

The S&L transport (`custom_components/nanit/aionanit_sl/`) was ported from
[nanit-sound-light](https://github.com/com6056/nanit-sound-light), where each
behavior below was reverse-engineered from the official app and validated
against a real speaker. Several of them look like optimization opportunities
or missing error handling. They are not. Undoing any of them re-introduces a
failure that was observed on real hardware:

- **One request in flight with await-ack, and NEVER re-send on a slow ack.**
  A slow ack means the speaker is busy, not gone. Re-sending piles duplicate
  commands onto it until it stops responding for ~30 seconds and then flushes
  the whole backlog at once. The official app never retries either. Only a
  socket drop or a non-2xx rejection fails a command.
- **No command-level retry, no app-level keepalive frame.** The speaker keeps
  its socket alive with WebSocket protocol ping (~20s) only. The camera path
  has both retry and a keepalive message. The speaker must not.
- **Combined `Settings` writes.** Multi-field commands coalesce into ONE
  message (`NanitSoundLight` handles this). Do not go back to one message per
  field: their out-of-order responses race and the device lands in the wrong
  state after a scene.
- **The backend readiness gate is sticky.** Wait for the relay's Connected
  frame before sending, but never detach on the bare/Disconnected backend
  frames the speaker emits periodically while fully usable. Only a socket
  drop detaches.
- **Light semantics (validated on speaker firmware 1.3.1):** the lamp emits
  iff `isOn && brightness > 0 && !noColor`. Light OFF is `brightness: 0`,
  which round-trips the stored color. The app's own `noColor` off relights in
  white on a bare re-enable, so light ON must always send explicit
  hue/saturation.
- **One local client per speaker.** The device 403s a second local
  WebSocket even with a fresh device token (verified on hardware). Don't
  diagnose a persistent local 403 as a wedged device without checking
  whether another client (a second HA, another integration) holds the slot.
- `sound_light_pb2.py` / `.pyi` are generated from `sound_light.proto`
  (protoc 29.5, enforced by the CI drift check). Never hand-edit them.

---

## CI

- **CI**: `.github/workflows/ci.yaml` runs on every PR and push to `main`, and is reused as the release gate. `CI OK` is the single required check that sums up the jobs (lint, types, tests, protobuf and card bundle drift, workflow lint, hassfest). HACS validation is informational.
- **PR**: `.github/workflows/pr.yaml` (`PR Metadata`) runs `tools/verify.py pr`: the title, the `## Changelog`, `## How I tested this` (live test, and a receipt that passed) and `## AI use` sections, and that GitHub verifies every commit's signature. Its job summary lists what the live test must show.
- **Auto beta**: `.github/workflows/auto-beta.yaml` tags labelled merges and dispatches the release.
- **Release**: `.github/workflows/release.yaml` resolves the tag, gates stable on owner approval, runs CI, builds, publishes aionanit to PyPI, then creates the GitHub release with nanit.zip and notes.

### Changelog

Release notes come from each PR's `## Changelog` section, see [docs/RELEASING.md](docs/RELEASING.md#release-notes). `CHANGELOG.md` is history up to 1.12.2; don't add entries to it.
