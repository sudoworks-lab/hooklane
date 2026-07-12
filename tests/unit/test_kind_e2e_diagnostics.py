from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from kind_e2e import sanitize_diagnostics  # noqa: E402


def test_diagnostics_remove_sensitive_lines() -> None:
    diagnostic = sanitize_diagnostics(
        "safe workload state\n"
        "payload: omitted-example\n"
        "authorization: omitted-example\n"
        "redis://example.invalid/0\n"
    )

    assert "safe workload state" in diagnostic
    assert "omitted-example" not in diagnostic
    assert "example.invalid" not in diagnostic
    assert diagnostic.count("[redacted diagnostic line]") == 3
