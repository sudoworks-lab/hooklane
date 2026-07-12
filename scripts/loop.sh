#!/usr/bin/env bash
# Goal loop Python runner launcher.

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(sys.version_info.major != 3)' >/dev/null 2>&1; then
  PYTHON=python
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info.major != 3)' >/dev/null 2>&1; then
  PYTHON=python3
else
  echo "error: Python 3 is required for feature isolation, timeout enforcement, and receipts; unsafe fallback execution is disabled." >&2
  exit 69
fi

exec "$PYTHON" "$SCRIPT_DIR/goal_loop_runner.py" "$@"
