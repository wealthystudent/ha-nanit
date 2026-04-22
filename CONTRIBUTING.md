# Contributing to ha-nanit

Thanks for your interest in contributing! This guide covers the human workflow.
For code standards, architecture details, and security requirements, see [AGENTS.md](AGENTS.md).

## Prerequisites

- Python 3.12+
- Home Assistant 2025.12+
- A Nanit account (for integration testing)

## Setup

```bash
git clone https://github.com/wealthystudent/ha-nanit.git
cd ha-nanit
just setup   # Installs deps, tooling, pre-commit hooks
```

## Development

### Running tests

```bash
just test          # Integration tests (config flow, migration, hub)
just test-lib      # aionanit library tests (protocol, REST, auth, transport)
just test-all      # Both
```

### Dev HA instance

```bash
just dev           # Start → http://localhost:8123
just dev restart   # Restart after code changes
just dev stop      # Stop
```

See [tests/README.md](tests/README.md) for more details.

## Making changes

### Git requirements

- **All commits must be GPG-signed.** PRs with unsigned commits will be rejected.
  Set up GPG signing: [GitHub docs](https://docs.github.com/en/authentication/managing-commit-signature-verification).
- **Squash merge only.** PR title becomes the commit message on `main`.
- **Pre-commit hooks** run ruff lint + format on every commit. Do not bypass with `--no-verify`.

### Workflow

1. **Branch** from `main`: `feat/<description>`, `fix/<description>`, or `chore/<description>`.
2. Make your changes. Follow existing code patterns.
3. Run `just check` (lint + format + typecheck + tests).
4. Open a **pull request** against `main`. PR title must follow conventional commit format (e.g., `feat: add night vision toggle`).
5. If the PR should trigger a release, add a label: `release:patch`, `release:minor`, or `release:major`.
6. CI must pass. If it fails, fix in the same branch and push.
7. Maintainer reviews and squash-merges. Branch is auto-deleted.

For full details on branching, commits, and signing: see [AGENTS.md → Git Workflow](AGENTS.md#git-workflow).

### Commit messages

We use [conventional commits](https://www.conventionalcommits.org/):

```
feat: add night vision toggle
fix: handle token refresh during reconnect
refactor: extract protobuf parsing into separate module
docs: update camera IP configuration instructions
test: add coverage for MFA config flow
chore: bump aionanit to 1.0.14
```

One logical change per commit. If behavior changes, update `README.md` in the same commit.

### Code style

- Fully async — no blocking I/O in the event loop.
- Type hints on all functions (mypy strict mode).
- User-facing text in `strings.json` / translations — no hardcoded English.
- Line length: 100 characters (enforced by Ruff).
- See [AGENTS.md → Code Standards](AGENTS.md#code-standards) for full details.

### Security

All contributions must pass security review. Key rules:

- Never log credentials, tokens, or stream URLs containing tokens.
- Validate user input with voluptuous schemas in config/options flows.
- Sanitize data from external APIs before using as entity names.
- No `eval()`, `exec()`, `os.system()`, or `subprocess(shell=True)`.
- Secrets in `entry.data` only, never in `entry.options`.
- Use `async_redact_data()` in diagnostics.

Full checklist: [`docs/SECURITY_AUDIT_CHECKLIST.md`](docs/SECURITY_AUDIT_CHECKLIST.md).

## Reporting issues

- **Bugs**: [Bug report template](https://github.com/wealthystudent/ha-nanit/issues/new?template=bug_report.yml)
- **Features**: [Feature request template](https://github.com/wealthystudent/ha-nanit/issues/new?template=feature_request.yml)

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
