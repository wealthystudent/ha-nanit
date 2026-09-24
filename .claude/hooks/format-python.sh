#!/usr/bin/env bash
# PostToolUse hook: format a Python file Claude just edited with the locked
# ruff, so edits match `just check` without a separate fix step. Format only:
# `ruff check --fix` here would delete imports added one edit before their use.
set -euo pipefail

file=$(python3 -c 'import json, sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))')
case "$file" in
  *.py | *.pyi) ;;
  *) exit 0 ;;
esac

cd "${CLAUDE_PROJECT_DIR:-.}"
uv run --frozen ruff format --quiet --force-exclude "$file" || true
