set quiet

python := "venv/bin/python3"

default:
    @just --list --unsorted

# ─── Setup & Quality ──────────────────────────────────────────────────

# Create venv and install all dependencies (everything stays in ./venv)
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d venv ]; then
        echo "Creating venv with python3.13 ..."
        python3.13 -m venv venv
    fi
    echo "Upgrading pip + setuptools ..."
    venv/bin/python3 -m pip install --upgrade pip setuptools wheel
    echo "Installing dependencies ..."
    venv/bin/python3 -m pip install -r dev/requirements.txt
    venv/bin/pre-commit install
    echo "Ready. Run 'just check' to verify."

# Run all checks (lint + format-check + typecheck + all tests) — local CI
check:
    venv/bin/ruff check .
    venv/bin/ruff format --check .
    venv/bin/mypy custom_components/nanit packages/aionanit/aionanit --config-file pyproject.toml
    {{ python }} -m pytest tests/unit/ -v --cov=custom_components/nanit --cov-fail-under=80
    {{ python }} -m pytest packages/aionanit/tests/ -v

# Auto-fix lint issues and reformat
fix:
    venv/bin/ruff check --fix .
    venv/bin/ruff format .

# ─── Testing ──────────────────────────────────────────────────────────

# Run tests: just test [lib|all] (default: integration with coverage)
test target="integration" *args="":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{ target }}" in
        integration) {{ python }} -m pytest tests/unit/ -v --cov=custom_components/nanit --cov-report=term-missing {{ args }} ;;
        lib)         {{ python }} -m pytest packages/aionanit/tests/ -v {{ args }} ;;
        all)         {{ python }} -m pytest tests/unit/ -v && {{ python }} -m pytest packages/aionanit/tests/ -v ;;
        *)           echo "Unknown target '{{ target }}'. Use: integration, lib, all"; exit 1 ;;
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
    {{ python }} tools/nanit-login.py {{ args }}

# Fetch activity events from Nanit cloud API
events *args:
    {{ python }} tools/nanit-events.py {{ args }}

# Interactive hardware probing tool (night light brightness discovery)
probe *args:
    {{ python }} tools/nanit-probe.py {{ args }}

# ─── Owner Only ───────────────────────────────────────────────────────

# [owner] Create a GitHub release: just release <patch|minor|major>
release bump:
    #!/usr/bin/env bash
    set -euo pipefail
    latest=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
    latest="${latest#v}"
    IFS='.' read -r major minor patch <<< "${latest}"
    case "{{ bump }}" in
        patch) patch=$((patch + 1)) ;;
        minor) minor=$((minor + 1)); patch=0 ;;
        major) major=$((major + 1)); minor=0; patch=0 ;;
        *) echo "Error: Invalid bump type '{{ bump }}'. Use patch, minor, or major."; exit 1 ;;
    esac
    new="${major}.${minor}.${patch}"
    tag="v${new}"
    echo "Bumping: ${latest} → ${new}"
    sed -i '' 's/"version": "[0-9]*\.[0-9]*\.[0-9]*"/"version": "'"${new}"'"/' custom_components/nanit/manifest.json
    sed -i '' 's/^version = "[0-9]*\.[0-9]*\.[0-9]*"/version = "'"${new}"'"/' packages/aionanit/pyproject.toml
    git add custom_components/nanit/manifest.json packages/aionanit/pyproject.toml
    git commit --no-gpg-sign -m "Bump version to ${new}"
    git tag "${tag}"
    git push && git push --tags
    gh release create "${tag}" --title "${tag}" --generate-notes
    echo "Released ${tag}"
