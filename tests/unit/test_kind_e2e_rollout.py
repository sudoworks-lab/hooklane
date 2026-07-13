from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import kind_e2e  # noqa: E402


def rollout_objects(
    *,
    rollout_complete: bool = True,
    old_replicas: int = 0,
    endpoint_revision: str = "current",
    endpoint_ready: bool = True,
    current_pod_name: str = "hooklane-mock-sink-current",
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    desired = 1
    deployment: dict[str, object] = {
        "metadata": {
            "uid": "deployment-uid",
            "generation": 4,
            "annotations": {"deployment.kubernetes.io/revision": "2"},
        },
        "spec": {"replicas": desired},
        "status": {
            "observedGeneration": 4 if rollout_complete else 3,
            "updatedReplicas": desired if rollout_complete else 0,
            "readyReplicas": desired if rollout_complete else 0,
            "availableReplicas": desired if rollout_complete else 0,
            "unavailableReplicas": 0 if rollout_complete else 1,
        },
    }
    replica_sets: list[dict[str, object]] = [
        {
            "metadata": {
                "name": "hooklane-mock-sink-current",
                "labels": {"pod-template-hash": "current"},
                "annotations": {"deployment.kubernetes.io/revision": "2"},
                "ownerReferences": [{"kind": "Deployment", "uid": "deployment-uid"}],
            },
            "spec": {"replicas": desired},
            "status": {
                "replicas": desired,
                "readyReplicas": desired,
                "availableReplicas": desired,
            },
        },
        {
            "metadata": {
                "name": "hooklane-mock-sink-old",
                "labels": {"pod-template-hash": "old"},
                "annotations": {"deployment.kubernetes.io/revision": "1"},
                "ownerReferences": [{"kind": "Deployment", "uid": "deployment-uid"}],
            },
            "spec": {"replicas": old_replicas},
            "status": {
                "replicas": old_replicas,
                "readyReplicas": old_replicas,
                "availableReplicas": old_replicas,
            },
        },
    ]
    pods: list[dict[str, object]] = [
        {
            "metadata": {
                "name": current_pod_name,
                "labels": {"pod-template-hash": "current"},
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
        {
            "metadata": {
                "name": "hooklane-mock-sink-old",
                "labels": {"pod-template-hash": "old"},
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
    ]
    endpoint_pod = (
        current_pod_name if endpoint_revision == "current" else "hooklane-mock-sink-old"
    )
    endpoint_slices: list[dict[str, object]] = [
        {
            "endpoints": [
                {
                    "conditions": {"ready": endpoint_ready},
                    "targetRef": {"kind": "Pod", "name": endpoint_pod},
                }
            ]
        }
    ]
    return deployment, replica_sets, pods, endpoint_slices


def evaluate(
    objects: tuple[
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ],
) -> tuple[bool, str]:
    deployment, replica_sets, pods, endpoint_slices = objects
    return kind_e2e.evaluate_mock_sink_rollout(
        deployment,
        replica_sets,
        pods,
        endpoint_slices,
    )


def test_rollout_waits_for_deployment_status() -> None:
    converged, summary = evaluate(rollout_objects(rollout_complete=False))

    assert not converged
    assert "Deployment generation is not observed" in summary


def test_rollout_waits_for_old_replica_set() -> None:
    converged, summary = evaluate(rollout_objects(old_replicas=1))

    assert not converged
    assert "old ReplicaSet still has active replicas" in summary


def test_rollout_waits_for_old_revision_endpoint() -> None:
    converged, summary = evaluate(rollout_objects(endpoint_revision="old"))

    assert not converged
    assert "Endpoint is not a Ready Pod from the current revision" in summary


def test_rollout_accepts_only_current_ready_revision() -> None:
    converged, summary = evaluate(rollout_objects())

    assert converged, summary


def test_rollout_timeout_is_nonzero_and_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    objects = rollout_objects(
        rollout_complete=False,
        current_pod_name="payload-credential",
    )
    monkeypatch.setattr(kind_e2e, "read_mock_sink_rollout_objects", lambda: objects)

    with pytest.raises(RuntimeError, match="mock sink rollout did not converge") as error:
        kind_e2e.wait_for_mock_sink_rollout(timeout_seconds=0, poll_seconds=0)

    message = str(error.value).lower()
    assert "payload-credential" not in message
    assert "[redacted]" in message
