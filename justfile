set quiet

default:
    @just --list --unsorted

# ─── Setup ────────────────────────────────────────────────────────────

# Install all dependencies (dev, test, aionanit, tooling)
setup:
    pip install -r dev/requirements.txt
    pre-commit install
    @echo "Ready. Run 'just check' to verify."

# ─── Quality ──────────────────────────────────────────────────────────

# Run ruff linter
lint *args:
    ruff check {{ args }} .

# Auto-format with ruff
format *args:
    ruff format {{ args }} .

# Run mypy type checker
typecheck:
    mypy custom_components/nanit packages/aionanit/aionanit --config-file pyproject.toml

# Run all checks (lint + format + typecheck + tests with coverage) — local CI
check:
    ruff check .
    ruff format --check .
    mypy custom_components/nanit packages/aionanit/aionanit --config-file pyproject.toml
    python3 -m pytest tests/unit/ -v --cov=custom_components/nanit --cov-fail-under=80
    python3 -m pytest packages/aionanit/tests/ -v

# ─── Testing ──────────────────────────────────────────────────────────

# Run integration tests (config flow, migration, hub)
test *args:
    python3 -m pytest tests/unit/ -v --cov=custom_components/nanit --cov-report=term-missing {{ args }}

# Run aionanit library tests
test-lib *args:
    python3 -m pytest packages/aionanit/tests/ -v {{ args }}

# Run all tests
test-all:
    python3 -m pytest tests/unit/ -v
    python3 -m pytest packages/aionanit/tests/ -v

# ─── Dev HA Instance ──────────────────────────────────────────────────

# Start dev HA instance (http://localhost:8123)
dev:
    docker compose -f dev/docker-compose.yml up -d
    @echo "HA running at http://localhost:8123"

# Stop dev HA instance
dev-stop:
    docker compose -f dev/docker-compose.yml down

# Restart dev HA (after code changes)
dev-restart:
    docker compose -f dev/docker-compose.yml restart homeassistant

# Tail dev HA logs (Ctrl+C to stop)
dev-logs:
    docker compose -f dev/docker-compose.yml logs -f homeassistant

# Wipe dev HA state for a fresh start
dev-reset:
    docker compose -f dev/docker-compose.yml down
    rm -rf dev/ha-config/.storage dev/ha-config/home-assistant_v2.db*
    @echo "Dev state wiped. Run 'just dev' to start fresh."

# ─── Tools ────────────────────────────────────────────────────────────

# Login to Nanit cloud (saves session for other tools)
login *args:
    python3 tools/nanit-login.py {{ args }}

# Fetch activity events from Nanit cloud API
events *args:
    python3 tools/nanit-events.py {{ args }}

# ─── Release ──────────────────────────────────────────────────────────

# Create a GitHub release: just release <patch|minor|major>
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
