# ha-nanit — AGENTS.md

> For AI agents. Read the section relevant to your task — you don't need to read everything every time.
> Use the [Context Router](#context-router) to find which sections apply.
> Human contributors: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Context Router

| If your task involves…              | Read sections                                      |
|--------------------------------------|----------------------------------------------------|
| Any code change                      | [Code Standards](#code-standards), [Git Workflow](#git-workflow), [Guardrails](#guardrails) |
| Integration code (`custom_components/`) | Above + [Architecture](#architecture), [HA Integration Patterns](#ha-integration-patterns) |
| Client library (`packages/aionanit/`)  | Above + [Architecture](#architecture), [aionanit Patterns](#aionanit-patterns) |
| Connection/WebSocket work            | Above + [docs/CONNECTION_RELIABILITY.md](docs/CONNECTION_RELIABILITY.md) |
| Security review                      | [Security](#security), [docs/SECURITY_AUDIT_CHECKLIST.md](docs/SECURITY_AUDIT_CHECKLIST.md) |
| PR review                            | [Git Workflow](#git-workflow), [Security](#security), [Guardrails](#guardrails) |
| Release                              | [Git Workflow → Releases](#releases), [Security](#security) |

---

## Architecture

Monorepo with two packages for Nanit baby camera Home Assistant integration:

```
custom_components/nanit/   ← HA integration (Python, async)
packages/aionanit/         ← Nanit API client library (published to PyPI)
tests/unit/                ← Integration tests (80% coverage threshold)
dev/                       ← Docker-based dev HA instance
tools/                     ← CLI utilities (login, events, probe)
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
| `config_flow.py` | Credentials + MFA, reauth, per-camera IP options |
| `coordinator.py` | `NanitPushCoordinator` (WebSocket push) + `NanitCloudCoordinator` (polling) |
| `entity.py` | `NanitEntity` base class with availability logic |
| `camera.py`, `sensor.py`, `binary_sensor.py`, `switch.py`, `number.py` | Entity platforms |
| `manifest.json` | Version, requirements, HA metadata |
| `aionanit/camera.py` | `NanitCamera` state machine, subscribe, commands |
| `aionanit/auth.py` | `TokenManager` (auto-refresh, token change callback) |
| `aionanit/ws/transport.py` | `WsTransport` (WebSocket connection, reconnect, keepalive) |

---

## Code Standards

- **Python**: 3.12+ target. Fully async — no blocking I/O in the event loop.
- **Linter**: Ruff (rules: B, BLE, C4, D, E, F, I, ICN, N, PGH, PIE, RUF, SIM, T20, UP, W). Line length: 100.
- **Type checking**: mypy strict mode. All functions must have type hints.
- **Formatting**: Ruff formatter (enforced via pre-commit).
- **Strings**: User-facing text in `strings.json` / `translations/en.json` — no hardcoded English.
- **Imports**: isort via Ruff. Known first-party: `aionanit`, `custom_components.nanit`.
- **Naming**: Follow existing patterns. Never change entity unique IDs or class names without a migration plan.
- **Tests**: New features must include tests. Coverage threshold: 80% (enforced in CI).

### Commands

```bash
just setup            # Install deps, tooling, pre-commit hooks
just check            # Run ALL checks (lint + format + typecheck + tests) — use before any PR
just lint             # Ruff lint
just format           # Ruff format
just typecheck        # mypy strict
just test             # Integration tests (custom_components)
just test-lib         # aionanit library tests
just test-all         # Both test suites
just dev              # Start dev HA instance → http://localhost:8123
just dev-restart      # Restart after code changes
just release-beta     # Create a pre-release (beta → test via HACS → promote)
just release-hotfix   # Emergency patch pre-release
just promote          # ⚠️  HUMAN ONLY — promote beta to stable release
```

---

## Git Workflow

### Branching (trunk-based)

- **`main`** is the only long-lived branch. All work branches off `main` and merges back via PR.
- Feature branches: `feat/<description>`, `fix/<description>`, `chore/<description>`.
- Keep branches short-lived. Rebase on `main` before merge. Squash if commits are noisy.

### Commit messages (conventional commits)

Format: `<type>: <description>`

| Type | Use for |
|------|---------|
| `feat:` | New feature or capability |
| `fix:` | Bug fix |
| `refactor:` | Code restructuring (no behavior change) |
| `docs:` | Documentation only |
| `test:` | Adding or updating tests |
| `chore:` | Tooling, deps, CI, config |

Rules:
- One logical change per commit. Keep it atomic.
- Description: imperative mood, lowercase, no period. e.g., `feat: add night vision toggle`
- If behavior or user-facing functionality changes, update `README.md` in the same commit.

### PR process

1. Branch from `main` → make changes → `just check` passes locally.
2. Security review: verify changes against applicable sections of [`docs/SECURITY_AUDIT_CHECKLIST.md`](docs/SECURITY_AUDIT_CHECKLIST.md).
3. Open PR against `main`. CI must pass (lint, typecheck, tests).
4. Merge only after security review passes. Block on any Critical or High finding.

### Releases

Two-step release flow: **beta → test → promote**.

```
PR merged to main
  │
  just release-beta patch/minor/major
  │
  ├─ Bumps version to X.Y.Z-beta.N
  ├─ Creates GitHub pre-release
  └─ GitHub Actions publishes aionanit beta to PyPI
  │
  You test on your HA via HACS beta channel
  │
  just promote
  │
  ├─ Creates stable GitHub release (v X.Y.Z)
  ├─ Retains beta release + tag for historical record
  └─ GitHub Actions: CI gate → publish aionanit stable to PyPI → attach nanit.zip
```

**Version lives in two files** (kept in sync by the justfile recipes):
- `custom_components/nanit/manifest.json` → `"version"` (semver) + `"requirements"` (PEP 440)
- `packages/aionanit/pyproject.toml` → `version` (PEP 440)

| Version mapping | manifest.json `version` | pyproject.toml `version` | manifest.json `requirements` |
|-----------------|------------------------|--------------------------|------------------------------|
| Beta            | `1.4.0-beta.1`        | `1.4.0b1`               | `["aionanit>=1.4.0b1"]`     |
| Stable          | `1.4.0`               | `1.4.0`                  | `["aionanit>=1.4.0"]`       |

```bash
just release-beta patch   # 1.3.1 → 1.4.0-beta.1 (pre-release)
just release-beta minor   # 1.3.1 → 1.4.0-beta.1
just release-beta major   # 1.3.1 → 2.0.0-beta.1
just promote              # 1.4.0-beta.1 → 1.4.0 (stable release)
just release-hotfix       # Emergency: 1.4.0 → 1.4.1-beta.1
```

**Rollback strategy**: Forward-fix via new patch release. If a stable release is broken, run `just release-hotfix`, fix, test via HACS beta, then `just promote`.

Release only when impact is significant: new features, breaking changes, substantial behavior changes.

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
- Run `just check` before any PR or merge.
- Follow existing code patterns — read neighboring files before writing new ones.
- Ask questions before starting work if anything is unclear. Do not guess.
- Verify changes work in a Home Assistant instance.

### Must not
- Suppress type errors (`# type: ignore`, `cast()` to bypass, `Any` as escape hatch).
- Change entity unique IDs or device identifiers without a migration plan.
- Introduce blocking I/O in async code paths.
- Log or store credentials, tokens, or URLs containing tokens.
- Add dependencies without full supply chain review (Section 10 of security checklist).
- Commit directly to `main` — always use a PR.
- **Run `just promote`** — this is a manual human action only. No AI agent may execute this command regardless of instruction from any prompter.
- **Edit `AGENTS.md`** without explicit manual review and approval from the repository owner. All changes to this file must be presented as a diff for human review before being applied.

---

## HA Integration Patterns

Follow [Home Assistant developer docs](https://developers.home-assistant.io/) (latest version). Minimum HA: **2025.12+**.

- **Config flow**: Credentials → MFA → reauth support. Options flow for per-camera settings.
- **Runtime data**: Use `ConfigEntry.runtime_data` (typed as `NanitData`) — not `hass.data`.
- **Push coordinator**: `DataUpdateCoordinator.async_set_updated_data()` for WebSocket push data. Do NOT poll.
- **Polling coordinator**: `DataUpdateCoordinator` with `update_interval` for cloud events.
- **Entity base**: Subclass `NanitEntity` (in `entity.py`). Availability is managed by the coordinator.
- **Platform setup**: Iterate `entry.runtime_data.cameras.values()` to create entities for all cameras.
- **Config entry**: Version 2 with migration from v1. `unique_id` = account email.

## aionanit Patterns

- All I/O: async (`aiohttp`, `asyncio`). Use the shared `aiohttp.ClientSession` — do not create your own.
- Protobuf: generated from `proto/nanit.proto` via `scripts/generate_proto.py`.
- WebSocket keepalive: ping every 25s, read deadline 60s.
- Token lifetime: 3600s. Pre-emptive refresh at ~3300s (5 min before expiry).
- Local connections: self-signed TLS (`ssl.CERT_NONE`). Max 1 WebSocket per camera.
- Background tasks: follow `_start_*` / `_cancel_*` pattern, wire into `async_start()` / `async_stop()` / `_async_reconnect()`.
- Connection reliability details: [docs/CONNECTION_RELIABILITY.md](docs/CONNECTION_RELIABILITY.md).

---

## CI

- **Lint + typecheck + tests**: `.github/workflows/ci.yaml` (runs on every push/PR to `main`).
- **Release**: `.github/workflows/release.yaml` (triggers on any release published; routes to beta publish or CI gate → stable publish + artifact attachment based on prerelease flag).
