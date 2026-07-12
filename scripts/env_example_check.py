"""Ensure the tracked environment example contains empty placeholders only."""

from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_KEYS = {"HOOKLANE_REDIS_URL", "HOOKLANE_CONSUMER_NAME"}
KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")


def main() -> int:
    path = ROOT / ".env.example"
    if not path.is_file():
        raise RuntimeError(".env.example is missing")

    keys: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator != "=" or not KEY_PATTERN.fullmatch(key):
            raise RuntimeError(f"invalid placeholder syntax at line {line_number}")
        if value:
            raise RuntimeError(f"placeholder value must be empty at line {line_number}")
        if key in keys:
            raise RuntimeError(f"duplicate placeholder at line {line_number}")
        keys.add(key)
    if keys != REQUIRED_KEYS:
        raise RuntimeError(".env.example keys do not match the runtime contract")
    print("[ok] .env.example contains empty placeholders only")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"[fail] environment example: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
