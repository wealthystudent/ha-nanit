set quiet

# Everything runs through uv: `uv run` keeps .venv in sync with uv.lock
# before each command, so there is no separate install step to forget.

default:
    @just --list --unsorted

# ─── Setup & Quality ──────────────────────────────────────────────────

# Install Python + all dev dependencies into .venv, and the git hooks
setup:
    uv sync
    uv run pre-commit install
    echo "Ready. Run 'just check' to verify."

# Run all checks (lint + format-check + typecheck + all tests) — local CI
check:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    uv run pytest tests/unit/ --cov=custom_components/nanit --cov-fail-under=80
    uv run pytest packages/aionanit/tests/

# Auto-fix lint issues and reformat
fix:
    uv run ruff check --fix .
    uv run ruff format .

# Upgrade all locked dependencies within their pinned ranges
upgrade:
    uv lock --upgrade
    echo "Review the uv.lock diff, then run 'just check'."

# ─── Testing ──────────────────────────────────────────────────────────

# Run tests: just test [lib|all] [pytest args] (default: integration with coverage)
test target="integration" *args="":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ target }}" in
        integration) uv run pytest tests/unit/ --cov=custom_components/nanit --cov-report=term-missing {{ args }} ;;
        lib)         uv run pytest packages/aionanit/tests/ {{ args }} ;;
        all)         uv run pytest tests/unit/ {{ args }} && uv run pytest packages/aionanit/tests/ {{ args }} ;;
        *)           echo "Unknown target '{{ target }}'. Use: integration, lib, all"; exit 1 ;;
    esac

# ─── Frontend ─────────────────────────────────────────────────────────

# Build the Lovelace card bundle (commit the result): just card [watch]
card action="build":
    #!/usr/bin/env bash
    set -euo pipefail
    cd frontend
    # Always install from the lockfile: a stale node_modules builds a bundle
    # that fails the CI drift check with a confusing diff.
    npm ci --ignore-scripts --no-audit --no-fund
    case "{{ action }}" in
        build) npm run build ;;
        watch) npm run watch ;;
        *)     echo "Unknown action '{{ action }}'. Use: build, watch"; exit 1 ;;
    esac

# ─── Dev HA Instance ──────────────────────────────────────────────────

# Dev HA: just dev [stop|restart|logs|reset] (default: start)
dev action="start":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ action }}" in
        start)   docker compose -f dev/docker-compose.yml up -d && echo "HA running at http://localhost:8123" ;;
        stop)    docker compose -f dev/docker-compose.yml down ;;
        restart) docker compose -f dev/docker-compose.yml restart homeassistant ;;
        logs)    docker compose -f dev/docker-compose.yml logs -f homeassistant ;;
        reset)   docker compose -f dev/docker-compose.yml down && rm -rf dev/ha-config/.storage dev/ha-config/home-assistant_v2.db* && echo "Dev state wiped. Run 'just dev' to start fresh." ;;
        *)       echo "Unknown action '{{ action }}'. Use: start, stop, restart, logs, reset"; exit 1 ;;
    esac

# ─── Tools ────────────────────────────────────────────────────────────

# Login to Nanit cloud (saves session for other tools)
login *args:
    uv run tools/nanit-login.py {{ args }}

# Fetch activity events from Nanit cloud API
events *args:
    uv run tools/nanit-events.py {{ args }}

# Interactive hardware probing tool (night light brightness discovery)
probe *args:
    uv run tools/nanit-probe.py {{ args }}

# Fetch camera network diagnostics (use --watch N to repeat)
network *args:
    uv run tools/nanit-network.py {{ args }}

# Probe sound machine / white noise API (interactive or single command)
sound *args:
    uv run tools/nanit-sound.py {{ args }}

# ─── Releases ────────────────────────────────────────────────────────

# Preview the release notes for what main would ship now
notes:
    uv run tools/release_notes.py --preview

# Interactive release CLI: create PR, label, merge, release stable (owner), retry.
# ⚠️  AI agents: DO NOT run this command. Manual human action only.
release:
    uv run tools/release-cli.py
