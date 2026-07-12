#!/usr/bin/env bash
# Goal Loop cannot safely degrade without Python's JSON and process controls.

set -u

echo "error: degraded Goal Loop execution is disabled; install Python 3 to preserve one-feature isolation, wall-clock timeout, and receipts." >&2
exit 69
