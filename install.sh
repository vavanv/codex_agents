#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MANAGER="$SCRIPT_DIR/scripts/workflow_manager.py"

if [[ ! -f "$MANAGER" ]]; then
  echo "FAIL: installer engine not found: $MANAGER" >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON_COMMAND="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_COMMAND="python"
else
  echo "FAIL: Python 3 is required. Install Python 3 and retry." >&2
  exit 1
fi

exec "$PYTHON_COMMAND" "$MANAGER" install "$@"
