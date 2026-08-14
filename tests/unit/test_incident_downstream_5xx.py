from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from incident_downstream_5xx import parse_args  # noqa: E402


def test_downstream_drill_defaults_to_no_normalized_output() -> None:
    args = parse_args([])

    assert args.normalized_output is None
    assert args.normalized_signal_id is None
    assert args.normalized_correlation_id is None
    assert args.normalized_observed_at is None


def test_downstream_drill_accepts_optional_normalized_output_parameters() -> None:
    args = parse_args(
        [
            "--normalized-output",
            "/tmp/downstream.json",
            "--normalized-signal-id",
            "signal-downstream-001",
            "--normalized-correlation-id",
            "correlation-downstream-001",
            "--normalized-observed-at",
            "2026-08-14T01:02:03.123Z",
        ]
    )

    assert args.normalized_output == Path("/tmp/downstream.json")
    assert args.normalized_signal_id == "signal-downstream-001"
    assert args.normalized_correlation_id == "correlation-downstream-001"
    assert args.normalized_observed_at == "2026-08-14T01:02:03.123Z"
