# Contributing to ha-nanit

Thanks for your interest in contributing! This guide will help you get started.

## Architecture

Read [AGENTS.md](AGENTS.md) first — it explains the repo structure, data flow, and development guidelines.

## Getting started

### Prerequisites

- Python 3.12+
- Home Assistant 2025.12+
- A Nanit account (for integration testing)

### Setup

```bash
# Clone the repo
git clone https://github.com/wealthystudent/ha-nanit.git
cd ha-nanit

# Install everything (deps, tooling, pre-commit hooks)
just setup
```

### Running tests

```bash
just test          # Integration tests (config flow, migration, hub)
just test-lib      # aionanit library tests (protocol, REST, auth, transport)
just test-all      # Both
```

### Dev HA instance

```bash
just dev           # Start → http://localhost:8123
just dev-restart   # Restart after code changes
just dev-stop      # Stop
```

See [tests/README.md](tests/README.md) for more details.

## Making changes

1. **Fork** the repo and create a feature branch.
2. Make your changes.
3. Run all checks: `just check` (lint + format + typecheck + tests)
4. Open a **pull request** against `main`.

### Code style

- All code must be **fully async** — no blocking I/O in the event loop.
- Follow existing patterns in the codebase.
- Use type hints everywhere.
- Use `strings.json` for user-facing text — no hardcoded English.
- Never log or store credentials/tokens unredacted.

### Commit messages

One task/feature per commit, with a concise description of what changed and why.

## Reporting issues

- **Bugs**: Use the [bug report template](https://github.com/wealthystudent/ha-nanit/issues/new?template=bug_report.yml).
- **Features**: Use the [feature request template](https://github.com/wealthystudent/ha-nanit/issues/new?template=feature_request.yml).

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
