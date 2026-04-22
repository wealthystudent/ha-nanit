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
# 1) PRs auto-create beta releases on merge (via auto-beta.yaml workflow)
# 2) validate in HACS beta channel
# 3) just promote [version] — promote a specific beta to stable
# Pipeline fix: just release-retry [tag] — re-triggers release workflow after fixing CI.

# ⚠️  AI agents: DO NOT run this command. Manual human action only.
promote version="":
    #!/usr/bin/env bash
    set -euo pipefail

    target="{{ version }}"

    if [ -z "${target}" ]; then
        # List all pre-releases grouped by base version, let user pick
        echo "Fetching pre-releases..."
        releases=$(gh release list --limit 50 --json tagName,isPrerelease,isDraft,publishedAt \
            --jq '[.[] | select(.isPrerelease and (.isDraft | not))]')

        if [ "$(echo "$releases" | jq 'length')" -eq 0 ]; then
            echo "Error: No pre-releases found to promote."
            exit 1
        fi

        # Extract unique base versions, sorted by version descending
        versions=$(echo "$releases" | jq -r '
            [.[] | .tagName | ltrimstr("v") | split("-beta.")[0]]
            | unique
            | sort_by(split(".") | map(tonumber))
            | reverse
            | .[]')

        echo ""
        echo "Available versions to promote:"
        echo "─────────────────────────────"
        i=1
        version_list=()
        while IFS= read -r v; do
            latest_beta=$(echo "$releases" | jq -r --arg v "$v" '
                [.[] | select(.tagName | startswith("v" + $v + "-beta."))]
                | sort_by(.tagName | split("-beta.")[1] | tonumber)
                | last | .tagName')
            beta_count=$(echo "$releases" | jq --arg v "$v" '
                [.[] | select(.tagName | startswith("v" + $v + "-beta."))] | length')
            echo "  ${i}) v${v}  (latest: ${latest_beta}, ${beta_count} beta(s))"
            version_list+=("$v")
            i=$((i + 1))
        done <<< "$versions"

        echo ""
        printf "Select version to promote [1-%d]: " "${#version_list[@]}"
        read -r choice

        if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#version_list[@]}" ]; then
            echo "Error: Invalid selection."
            exit 1
        fi
        target="${version_list[$((choice - 1))]}"
    fi

    # Find the latest beta tag for this version
    beta_tag=$(git tag -l "v${target}-beta.*" | sort -V | tail -1)

    if [ -z "${beta_tag}" ]; then
        echo "Error: No beta tags found for version ${target}."
        echo "Available beta tags:"
        git tag -l 'v*-beta.*' | sort -V
        exit 1
    fi

    echo ""
    echo "Promoting ${beta_tag} → v${target}"
    echo ""

    # Verify the beta tag exists and is reachable
    beta_sha=$(git rev-list -n1 "${beta_tag}" 2>/dev/null || true)
    if [ -z "${beta_sha}" ]; then
        echo "Error: Could not resolve commit for tag ${beta_tag}."
        exit 1
    fi

    # Update version files to stable
    sed -E -i '' 's/"version": "[^"]+"/"version": "'"${target}"'"/' custom_components/nanit/manifest.json
    sed -E -i '' 's/^version = "[^"]+"/version = "'"${target}"'"/' packages/aionanit/pyproject.toml
    sed -E -i '' 's/"aionanit>=[^"]*"/"aionanit>='"${target}"'"/' custom_components/nanit/manifest.json

    git add custom_components/nanit/manifest.json packages/aionanit/pyproject.toml
    git commit -m "chore: bump version to ${target}"
    git tag -m "v${target}" "v${target}"
    git push && git push --tags
    gh release create "v${target}" --title "v${target}" --generate-notes --latest
    echo ""
    echo "✅ v${target} released. GitHub Actions will publish to PyPI and attach artifacts."

release-retry tag="":
    #!/usr/bin/env bash
    set -euo pipefail
    retry_tag="{{ tag }}"
    if [ -z "${retry_tag}" ]; then
        retry_tag=$(gh release list --limit 1 --json tagName --jq '.[0].tagName')
        if [ -z "${retry_tag}" ] || [ "${retry_tag}" = "null" ]; then
            echo "Error: No release found. Provide a tag: just release-retry v1.2.3-beta.1"
            exit 1
        fi
    fi
    echo "Re-triggering release workflow for ${retry_tag} ..."
    gh workflow run release.yaml -f tag_name="${retry_tag}"
    echo "Dispatched. Watch: gh run list --workflow release.yaml --limit 1"
