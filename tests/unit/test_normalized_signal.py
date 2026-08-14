from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hooklane.observability.normalized_signal import (
    WorkerUnavailableObservation,
    build_worker_unavailable_signal,
    normalize_worker_unavailable,
)


def worker_observation() -> dict[str, object]:
    return {
        "signal_id": "signal-worker-001",
        "observed_at": "2026-08-14T01:02:03.123Z",
        "correlation_id": "correlation-worker-001",
        "alert_state": "firing",
        "target_present": False,
        "readiness_present": False,
        "available_instances": 0,
        "required_instances": 1,
        "required_duration_seconds": 15.0,
        "source_ref": "docs/incidents/worker-stop.md",
        "captured_at": "2026-08-14T01:02:03.123Z",
        "evidence_refs": [
            "docs/incidents/worker-stop.md",
            "docs/runbooks/HooklaneWorkerUnavailable.md",
        ],
    }


def test_worker_unavailable_maps_to_current_normalized_schema() -> None:
    signal = build_worker_unavailable_signal(worker_observation())

    assert signal["kind"] == "normalized_operational_signal"
    assert signal["schema_version"] == "1.0"
    assert signal["signal_type"] == "service.availability"
    assert signal["detector_id"] == "HooklaneWorkerUnavailable"
    assert signal["scope"] == {"service": "hooklane", "components": ["worker"]}
    assert signal["evaluation"] == {
        "state": "firing",
        "required_duration_seconds": 15.0,
    }
    assert signal["observation"] == {
        "kind": "condition",
        "state": "unavailable",
        "expected_state": "available",
        "reachability": "unreachable",
        "readiness": "not_ready",
        "available_instances": 0,
        "required_instances": 1,
    }
    assert signal["source"] == {
        "system": "hooklane",
        "scenario": "worker-stop",
        "source_ref": "docs/incidents/worker-stop.md",
        "correlation_id": "correlation-worker-001",
        "synthetic": True,
        "captured_at": "2026-08-14T01:02:03.123Z",
    }


def test_worker_unavailable_preserves_caller_values_and_is_byte_deterministic() -> None:
    observation = worker_observation()

    first = normalize_worker_unavailable(observation)
    second = normalize_worker_unavailable(observation)
    parsed = json.loads(first)

    assert first == second
    assert first.endswith(b"\n")
    assert parsed["signal_id"] == "signal-worker-001"
    assert parsed["observed_at"] == "2026-08-14T01:02:03.123Z"
    assert parsed["source"]["correlation_id"] == "correlation-worker-001"
    assert parsed["evaluation"]["required_duration_seconds"] == 15.0


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_refs": []},
        {"target_present": True},
        {"available_instances": 1},
        {"required_instances": 2},
        {"payload": "must not cross the boundary"},
        {"evidence_refs": ["promql/worker"]},
    ],
)
def test_worker_unavailable_rejects_incomplete_or_unsafe_observation(
    changes: dict[str, object],
) -> None:
    observation = worker_observation()
    observation.update(changes)

    with pytest.raises(ValidationError):
        WorkerUnavailableObservation.model_validate(observation)


def test_normalized_output_contains_no_forbidden_operational_data() -> None:
    output = normalize_worker_unavailable(worker_observation()).decode("utf-8")

    assert "promql" not in output.lower()
    assert "payload" not in output.lower()
    assert "redis://" not in output.lower()
    assert "idempotency" not in output.lower()
