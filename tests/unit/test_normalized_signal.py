from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hooklane.observability.normalized_signal import (
    DeliveryFailureRateObservation,
    WorkerUnavailableObservation,
    build_delivery_failure_rate_signal,
    build_worker_unavailable_signal,
    normalize_delivery_failure_rate,
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


def delivery_failure_rate_observation() -> dict[str, object]:
    return {
        "signal_id": "signal-downstream-001",
        "observed_at": "2026-08-14T01:02:03.123Z",
        "correlation_id": "correlation-downstream-001",
        "alert_state": "pending",
        "failure_rate": 0.75,
        "threshold": 0.20,
        "window_seconds": 30.0,
        "required_duration_seconds": 10.0,
        "source_ref": "docs/incidents/downstream-5xx.md",
        "captured_at": "2026-08-14T01:02:03.123Z",
        "evidence_refs": [
            "docs/incidents/downstream-5xx.md",
            "docs/runbooks/HooklaneDeliveryFailureRateHigh.md",
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


def test_delivery_failure_rate_maps_to_current_normalized_scalar_schema() -> None:
    signal = build_delivery_failure_rate_signal(delivery_failure_rate_observation())

    assert signal["kind"] == "normalized_operational_signal"
    assert signal["schema_version"] == "1.0"
    assert signal["signal_type"] == "delivery.failure_rate"
    assert signal["detector_id"] == "HooklaneDeliveryFailureRateHigh"
    assert signal["scope"] == {"service": "hooklane", "components": ["worker"]}
    assert signal["evaluation"] == {
        "state": "pending",
        "required_duration_seconds": 10.0,
        "window_seconds": 30.0,
    }
    assert signal["observation"] == {
        "kind": "scalar",
        "measurement": "ratio",
        "value": 0.75,
        "unit": "ratio",
        "comparison": "gt",
        "threshold": 0.20,
    }
    assert signal["source"] == {
        "system": "hooklane",
        "scenario": "downstream-5xx",
        "source_ref": "docs/incidents/downstream-5xx.md",
        "correlation_id": "correlation-downstream-001",
        "synthetic": True,
        "captured_at": "2026-08-14T01:02:03.123Z",
    }


def test_delivery_failure_rate_preserves_caller_values_and_is_byte_deterministic() -> None:
    observation = delivery_failure_rate_observation()

    first = normalize_delivery_failure_rate(observation)
    second = normalize_delivery_failure_rate(observation)
    parsed = json.loads(first)

    assert first == second
    assert first.endswith(b"\n")
    assert parsed["signal_id"] == "signal-downstream-001"
    assert parsed["observed_at"] == "2026-08-14T01:02:03.123Z"
    assert parsed["evaluation"]["required_duration_seconds"] == 10.0
    assert parsed["evaluation"]["window_seconds"] == 30.0
    assert parsed["observation"]["value"] == 0.75


@pytest.mark.parametrize(
    "changes",
    [
        {"failure_rate": 0.20},
        {"failure_rate": 1.1},
        {"window_seconds": 0},
        {"evidence_refs": []},
        {"payload": "must not cross the boundary"},
        {"evidence_refs": ["promql/downstream"]},
    ],
)
def test_delivery_failure_rate_rejects_invalid_or_unsafe_observation(
    changes: dict[str, object],
) -> None:
    observation = delivery_failure_rate_observation()
    observation.update(changes)

    with pytest.raises(ValidationError):
        DeliveryFailureRateObservation.model_validate(observation)


def test_delivery_failure_rate_output_contains_no_forbidden_operational_data() -> None:
    output = normalize_delivery_failure_rate(delivery_failure_rate_observation()).decode("utf-8")

    assert "promql" not in output.lower()
    assert "payload" not in output.lower()
    assert "redis://" not in output.lower()
    assert "idempotency" not in output.lower()
