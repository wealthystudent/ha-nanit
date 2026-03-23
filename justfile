# Nanit HA Integration
# Show available commands
default:
    @just --list

# --- Development ---

# Start dev HA instance (http://localhost:8123)
dev:
    docker-compose -f docker-compose.dev.yml up -d
    @echo "HA running at http://localhost:8123"

# Stop dev HA instance
dev-stop:
    docker-compose -f docker-compose.dev.yml down

# Restart dev HA (after code changes)
dev-restart:
    docker-compose -f docker-compose.dev.yml restart homeassistant

# Tail dev HA logs (Ctrl+C to stop)
dev-logs:
    docker-compose -f docker-compose.dev.yml logs -f homeassistant

# Wipe dev HA state for a fresh start
dev-reset:
    docker-compose -f docker-compose.dev.yml down
    rm -rf dev-config/.storage dev-config/home-assistant_v2.db*
    @echo "Dev state wiped. Run 'just dev' to start fresh."

# --- Testing ---

# Run integration tests (config flow, migration, hub)
test:
    python3 -m pytest tests/unit/ -v

# Run aionanit library tests
test-lib:
    cd packages/aionanit && python3 -m pytest tests/ -v

# Run all tests
test-all:
    python3 -m pytest tests/unit/ -v
    cd packages/aionanit && python3 -m pytest tests/ -v

# --- Tools ---

# Login to Nanit cloud (saves session to .nanit-session)
login *args:
    python3 tools/nanit-login.py {{args}}

# Fetch activity events from Nanit cloud API (default: 10)
# Examples: just events    |    just events --limit 5
events *args:
    python3 tools/nanit-events.py {{args}}

# --- Release ---

# Create a GitHub release by bumping the latest version.
# Usage: just release <patch|minor|major>
# Example: just release patch  (0.2.1 → 0.2.2)
release bump:
    #!/usr/bin/env bash
    set -euo pipefail
    # Get latest version tag, strip 'v' prefix
    latest=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
    latest="${latest#v}"
    IFS='.' read -r major minor patch <<< "${latest}"
    case "{{bump}}" in
        patch) patch=$((patch + 1)) ;;
        minor) minor=$((minor + 1)); patch=0 ;;
        major) major=$((major + 1)); minor=0; patch=0 ;;
        *) echo "Error: Invalid bump type '{{bump}}'. Use patch, minor, or major."; exit 1 ;;
    esac
    new="${major}.${minor}.${patch}"
    tag="v${new}"
    echo "Bumping: ${latest} → ${new}"
    # Update manifest.json
    sed -i '' 's/"version": "[0-9]*\.[0-9]*\.[0-9]*"/"version": "'"${new}"'"/' custom_components/nanit/manifest.json
    # Update aionanit pyproject.toml
    sed -i '' 's/^version = "[0-9]*\.[0-9]*\.[0-9]*"/version = "'"${new}"'"/' packages/aionanit/pyproject.toml
    git add custom_components/nanit/manifest.json packages/aionanit/pyproject.toml
    git commit -m "Bump version to ${new}"
    git tag "${tag}"
    git push && git push --tags
    gh release create "${tag}" --title "${tag}" --generate-notes
    echo "Released ${tag}"
