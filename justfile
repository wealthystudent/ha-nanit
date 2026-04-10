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

# ─── Releases (Owner Only) ────────────────────────────────────────────

# Release flow:
# 1) just release-beta <patch|minor|major>
# 2) validate in HACS beta channel
# 3) just promote
# Hotfix flow: just release-hotfix, test, then just promote.

release-beta bump:
    #!/usr/bin/env bash
    set -euo pipefail
    latest=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
    latest="${latest#v}"
    latest="${latest%%-*}"
    IFS='.' read -r major minor patch <<< "${latest}"
    case "{{ bump }}" in
        patch) patch=$((patch + 1)) ;;
        minor) minor=$((minor + 1)); patch=0 ;;
        major) major=$((major + 1)); minor=0; patch=0 ;;
        *) echo "Error: Invalid bump type '{{ bump }}'. Use patch, minor, or major."; exit 1 ;;
    esac
    new="${major}.${minor}.${patch}"
    beta_num=1
    for t in $(git tag -l "v${new}-beta.*"); do
        n="${t##*-beta.}"
        if [ "${n}" -ge "${beta_num}" ]; then
            beta_num=$((n + 1))
        fi
    done
    semver_beta="${new}-beta.${beta_num}"
    pep440_beta="${major}.${minor}.${patch}b${beta_num}"
    tag="v${semver_beta}"
    sed -E -i '' 's/"version": "[^"]+"/"version": "'"${semver_beta}"'"/' custom_components/nanit/manifest.json
    sed -E -i '' 's/^version = "[^"]+"/version = "'"${pep440_beta}"'"/' packages/aionanit/pyproject.toml
    sed -E -i '' 's/"aionanit>=[^"]*"/"aionanit>='"${pep440_beta}"'"/' custom_components/nanit/manifest.json
    git add custom_components/nanit/manifest.json packages/aionanit/pyproject.toml
    git commit --no-gpg-sign -m "chore: bump version to ${semver_beta}"
    git tag "${tag}"
    git push && git push --tags
    gh release create "${tag}" --title "${tag}" --generate-notes --prerelease --latest=false
    echo "Pre-release ${tag} created. Test via HACS beta channel, then run: just promote"

# ⚠️  AI agents: DO NOT run this command. Manual human action only.
promote:
    #!/usr/bin/env bash
    set -euo pipefail
    beta_tag=$(gh release list --limit 20 --json tagName,isPreRelease,isDraft --jq '[.[] | select(.isPreRelease and (.isDraft | not))] | first | .tagName')
    if [ -z "${beta_tag}" ] || [ "${beta_tag}" = "null" ]; then
        echo "Error: No pre-release found to promote."
        exit 1
    fi
    stable="${beta_tag#v}"
    stable="${stable%-beta.*}"
    beta_sha=$(git rev-list -n1 "${beta_tag}")
    if [ -z "${beta_sha}" ]; then
        echo "Error: Could not resolve commit for tag ${beta_tag}."
        exit 1
    fi
    sed -E -i '' 's/"version": "[^"]+"/"version": "'"${stable}"'"/' custom_components/nanit/manifest.json
    sed -E -i '' 's/^version = "[^"]+"/version = "'"${stable}"'"/' packages/aionanit/pyproject.toml
    sed -E -i '' 's/"aionanit>=[^"]*"/"aionanit>='"${stable}"'"/' custom_components/nanit/manifest.json
    git add custom_components/nanit/manifest.json packages/aionanit/pyproject.toml
    git commit --no-gpg-sign -m "chore: bump version to ${stable}"
    git tag "v${stable}"
    git push && git push --tags
    gh release delete "${beta_tag}" --yes
    git push origin --delete "${beta_tag}"
    git tag -d "${beta_tag}"
    gh release create "v${stable}" --title "v${stable}" --generate-notes --latest
    echo "✅ v${stable} released. GitHub Actions will publish to PyPI and attach artifacts."

release-hotfix:
    #!/usr/bin/env bash
    set -euo pipefail
    latest="v0.0.0"
    for t in $(git tag --sort=-v:refname); do
        case "${t}" in
            v*-beta.*) ;;
            v[0-9]*.[0-9]*.[0-9]*) latest="${t}"; break ;;
        esac
    done
    latest="${latest#v}"
    IFS='.' read -r major minor patch <<< "${latest}"
    patch=$((patch + 1))
    new="${major}.${minor}.${patch}"
    beta_num=1
    for t in $(git tag -l "v${new}-beta.*"); do
        n="${t##*-beta.}"
        if [ "${n}" -ge "${beta_num}" ]; then
            beta_num=$((n + 1))
        fi
    done
    semver_beta="${new}-beta.${beta_num}"
    pep440_beta="${major}.${minor}.${patch}b${beta_num}"
    tag="v${semver_beta}"
    sed -E -i '' 's/"version": "[^"]+"/"version": "'"${semver_beta}"'"/' custom_components/nanit/manifest.json
    sed -E -i '' 's/^version = "[^"]+"/version = "'"${pep440_beta}"'"/' packages/aionanit/pyproject.toml
    sed -E -i '' 's/"aionanit>=[^"]*"/"aionanit>='"${pep440_beta}"'"/' custom_components/nanit/manifest.json
    git add custom_components/nanit/manifest.json packages/aionanit/pyproject.toml
    git commit --no-gpg-sign -m "chore: bump version to ${semver_beta}"
    git tag "${tag}"
    git push && git push --tags
    gh release create "${tag}" --title "${tag}" --generate-notes --prerelease --latest=false
    echo "Hotfix pre-release ${tag} created. Test, then run: just promote"
