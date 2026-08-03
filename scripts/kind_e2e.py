"""Run Hooklane's normal, retry, pending recovery, status, and metrics kind E2E."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Never, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import kind_runtime
import observability_runtime


ROOT = Path(__file__).resolve().parents[1]
API_ORIGIN = "http://127.0.0.1:18082"
MOCK_SINK_DEPLOYMENT = "hooklane-mock-sink"
MOCK_SINK_SERVICE = "hooklane-mock-sink"
MOCK_SINK_SELECTOR = (
    f"app.kubernetes.io/instance={kind_runtime.RELEASE},"
    "app.kubernetes.io/component=mock-sink"
)
DEPLOYMENT_REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
POD_TEMPLATE_HASH_LABEL = "pod-template-hash"
DIAGNOSTIC_FORBIDDEN = re.compile(
    r"payload|idempotency-key|cookie|credential|password|private key|redis://|authorization:",
    flags=re.IGNORECASE,
)


def fail(message: str) -> Never:
    raise RuntimeError(message)


def request_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    request = Request(
        f"{API_ORIGIN}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=5) as response:
            parsed: object = json.loads(response.read())
            if not isinstance(parsed, dict):
                fail("API response is not an object")
            return response.status, cast(dict[str, Any], parsed)
    except HTTPError as error:
        parsed = json.loads(error.read())
        if not isinstance(parsed, dict):
            fail("API error response is not an object")
        return error.code, cast(dict[str, Any], parsed)


def wait_api_ready() -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            status, _response = request_json("GET", "/health/ready")
        except OSError:
            time.sleep(0.5)
            continue
        if status == 200:
            return
        time.sleep(0.5)
    fail("API did not become ready")


def post_event(event_type: str, *, idempotency_key: str | None = None) -> str:
    headers = None if idempotency_key is None else {"Idempotency-Key": idempotency_key}
    status, response = request_json(
        "POST",
        "/v1/events",
        body={"event_type": event_type, "payload": {}},
        headers=headers,
    )
    event_id = response.get("event_id")
    if status != 202 or not isinstance(event_id, str):
        fail("event acceptance did not return 202 and an event ID")
    return event_id


def event_status(event_id: str) -> dict[str, Any]:
    status, record = request_json("GET", f"/v1/events/{event_id}")
    if status != 200 or record.get("event_id") != event_id:
        fail("status API response is inconsistent")
    return record


def wait_event_state(
    event_id: str,
    expected: str,
    *,
    timeout_seconds: float = 90,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_state: object = None
    while time.monotonic() < deadline:
        record = event_status(event_id)
        last_state = record.get("status")
        if last_state == expected:
            return record
        if last_state == "dead_letter":
            fail("event reached dead-letter during E2E recovery")
        time.sleep(0.1)
    fail(f"event did not reach {expected}; last state was {last_state}")


def kubectl_command(*arguments: str) -> list[str]:
    return [
        "kubectl",
        "--kubeconfig",
        str(kind_runtime.KUBECONFIG),
        "--context",
        kind_runtime.CONTEXT_NAME,
        *arguments,
    ]


def run_kubectl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        kubectl_command(*arguments),
        cwd=ROOT,
        check=check,
        text=True,
    )


def kubectl_json(*arguments: str) -> dict[str, Any]:
    completed = subprocess.run(
        kubectl_command(*arguments, "-o", "json"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    parsed: object = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        fail("kubectl JSON response is not an object")
    return cast(dict[str, Any], parsed)


def wait_rollout(resource: str, *, timeout: str = "240s") -> None:
    run_kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "rollout",
        "status",
        resource,
        f"--timeout={timeout}",
    )


def object_items(document: dict[str, Any], label: str) -> list[dict[str, Any]]:
    raw_items = document.get("items")
    if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
        fail(f"{label} list is malformed")
    return cast(list[dict[str, Any]], raw_items)


def pod_ready(pod: dict[str, Any]) -> bool:
    status = pod.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    return isinstance(conditions, list) and any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )


def replica_count(mapping: object, key: str) -> int:
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, int) else 0


def owned_by_deployment(replica_set: dict[str, Any], deployment_uid: object) -> bool:
    metadata = replica_set.get("metadata")
    owner_references = metadata.get("ownerReferences") if isinstance(metadata, dict) else None
    return isinstance(deployment_uid, str) and isinstance(owner_references, list) and any(
        isinstance(owner, dict)
        and owner.get("kind") == "Deployment"
        and owner.get("uid") == deployment_uid
        for owner in owner_references
    )


def diagnostic_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return "[redacted]" if DIAGNOSTIC_FORBIDDEN.search(value) else value


def read_mock_sink_rollout_objects(
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    deployment = kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "deployment",
        MOCK_SINK_DEPLOYMENT,
    )
    replica_sets = object_items(
        kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "replicasets",
            "--selector",
            MOCK_SINK_SELECTOR,
        ),
        "mock sink ReplicaSet",
    )
    pods = object_items(
        kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "pods",
            "--selector",
            MOCK_SINK_SELECTOR,
        ),
        "mock sink Pod",
    )
    endpoint_slices = object_items(
        kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "endpointslices",
            "--selector",
            f"kubernetes.io/service-name={MOCK_SINK_SERVICE}",
        ),
        "mock sink EndpointSlice",
    )
    return deployment, replica_sets, pods, endpoint_slices


def evaluate_mock_sink_rollout(
    deployment: dict[str, Any],
    replica_sets: list[dict[str, Any]],
    pods: list[dict[str, Any]],
    endpoint_slices: list[dict[str, Any]],
) -> tuple[bool, str]:
    metadata = deployment.get("metadata")
    spec = deployment.get("spec")
    status = deployment.get("status")
    if not isinstance(metadata, dict) or not isinstance(spec, dict) or not isinstance(status, dict):
        fail("mock sink Deployment status is malformed")

    reasons: list[str] = []
    generation = metadata.get("generation")
    desired = spec.get("replicas")
    observed = status.get("observedGeneration")
    updated = status.get("updatedReplicas", 0)
    ready = status.get("readyReplicas", 0)
    available = status.get("availableReplicas", 0)
    unavailable = status.get("unavailableReplicas", 0)
    if not isinstance(desired, int) or desired < 1:
        reasons.append("Deployment desired replicas are invalid")
    else:
        if observed != generation:
            reasons.append("Deployment generation is not observed")
        if updated != desired:
            reasons.append("Deployment updated replicas do not match desired")
        if ready != desired:
            reasons.append("Deployment ready replicas do not match desired")
        if available != desired:
            reasons.append("Deployment available replicas do not match desired")
        if unavailable not in (None, 0):
            reasons.append("Deployment still has unavailable replicas")

    deployment_uid = metadata.get("uid")
    annotations = metadata.get("annotations")
    current_revision = (
        annotations.get(DEPLOYMENT_REVISION_ANNOTATION)
        if isinstance(annotations, dict)
        else None
    )
    current_hashes: set[str] = set()
    replica_summaries: list[dict[str, Any]] = []
    for replica_set in replica_sets:
        if not owned_by_deployment(replica_set, deployment_uid):
            continue
        replica_metadata = replica_set.get("metadata")
        replica_spec = replica_set.get("spec")
        replica_status = replica_set.get("status")
        if not isinstance(replica_metadata, dict):
            reasons.append("ReplicaSet metadata is malformed")
            continue
        replica_labels = replica_metadata.get("labels")
        replica_annotations = replica_metadata.get("annotations")
        template_hash = (
            replica_labels.get(POD_TEMPLATE_HASH_LABEL)
            if isinstance(replica_labels, dict)
            else None
        )
        revision = (
            replica_annotations.get(DEPLOYMENT_REVISION_ANNOTATION)
            if isinstance(replica_annotations, dict)
            else None
        )
        counts = {
            "desired": replica_count(replica_spec, "replicas"),
            "current": replica_count(replica_status, "replicas"),
            "ready": replica_count(replica_status, "readyReplicas"),
            "available": replica_count(replica_status, "availableReplicas"),
        }
        active_replicas = max(counts.values())
        replica_summaries.append(
            {
                "name": diagnostic_identifier(replica_metadata.get("name")),
                "revision": diagnostic_identifier(revision),
                "templateHash": diagnostic_identifier(template_hash),
                "replicas": counts,
            }
        )
        if revision == current_revision and isinstance(template_hash, str):
            current_hashes.add(template_hash)
        elif active_replicas != 0:
            reasons.append("old ReplicaSet still has active replicas")

    if not isinstance(current_revision, str) or len(current_hashes) != 1:
        reasons.append("current Deployment revision does not map to one ReplicaSet")
        current_hash = None
    else:
        current_hash = next(iter(current_hashes))

    pod_by_name: dict[str, dict[str, Any]] = {}
    current_ready_pods: set[str] = set()
    pod_summaries: list[dict[str, Any]] = []
    for pod in pods:
        pod_metadata = pod.get("metadata")
        if not isinstance(pod_metadata, dict):
            reasons.append("Pod metadata is malformed")
            continue
        name = pod_metadata.get("name")
        labels = pod_metadata.get("labels")
        template_hash = labels.get(POD_TEMPLATE_HASH_LABEL) if isinstance(labels, dict) else None
        is_ready = pod_ready(pod)
        deleting = pod_metadata.get("deletionTimestamp") is not None
        pod_summaries.append(
            {
                "name": diagnostic_identifier(name),
                "templateHash": diagnostic_identifier(template_hash),
                "ready": is_ready,
                "deleting": deleting,
            }
        )
        if isinstance(name, str):
            pod_by_name[name] = pod
            if template_hash == current_hash and is_ready and not deleting:
                current_ready_pods.add(name)

    if isinstance(desired, int) and len(current_ready_pods) != desired:
        reasons.append("current revision Ready Pod count does not match desired")

    endpoint_pods: set[str] = set()
    endpoint_summaries: list[dict[str, Any]] = []
    for endpoint_slice in endpoint_slices:
        raw_endpoints = endpoint_slice.get("endpoints")
        if not isinstance(raw_endpoints, list):
            reasons.append("EndpointSlice endpoints are malformed")
            continue
        for endpoint in raw_endpoints:
            if not isinstance(endpoint, dict):
                reasons.append("EndpointSlice endpoint is malformed")
                continue
            conditions = endpoint.get("conditions")
            target_ref = endpoint.get("targetRef")
            endpoint_ready = (
                isinstance(conditions, dict) and conditions.get("ready") is True
            )
            pod_name = (
                target_ref.get("name")
                if isinstance(target_ref, dict) and target_ref.get("kind") == "Pod"
                else None
            )
            endpoint_summaries.append(
                {"pod": diagnostic_identifier(pod_name), "ready": endpoint_ready}
            )
            if not isinstance(pod_name, str):
                reasons.append("Endpoint does not identify a Pod")
                continue
            endpoint_pods.add(pod_name)
            endpoint_pod = pod_by_name.get(pod_name)
            pod_metadata = (
                endpoint_pod.get("metadata") if isinstance(endpoint_pod, dict) else None
            )
            pod_labels = pod_metadata.get("labels") if isinstance(pod_metadata, dict) else None
            endpoint_hash = (
                pod_labels.get(POD_TEMPLATE_HASH_LABEL)
                if isinstance(pod_labels, dict)
                else None
            )
            if (
                endpoint_hash != current_hash
                or not endpoint_ready
                or not isinstance(endpoint_pod, dict)
                or not pod_ready(endpoint_pod)
                or (
                    isinstance(pod_metadata, dict)
                    and pod_metadata.get("deletionTimestamp") is not None
                )
            ):
                reasons.append("Endpoint is not a Ready Pod from the current revision")

    if endpoint_pods != current_ready_pods:
        reasons.append("Service Endpoints do not match current revision Ready Pods")

    summary = {
        "deployment": {
            "generation": generation,
            "observedGeneration": observed,
            "desiredReplicas": desired,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
            "unavailableReplicas": unavailable,
            "currentRevision": diagnostic_identifier(current_revision),
            "currentTemplateHash": diagnostic_identifier(current_hash),
        },
        "replicaSets": replica_summaries,
        "pods": pod_summaries,
        "endpoints": endpoint_summaries,
        "reasons": sorted(set(reasons)),
    }
    sanitized_summary = sanitize_diagnostics(json.dumps(summary, sort_keys=True)).strip()
    return not reasons, sanitized_summary


def wait_for_mock_sink_rollout(
    *,
    timeout_seconds: float = 60,
    poll_seconds: float = 0.2,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        converged, summary = evaluate_mock_sink_rollout(*read_mock_sink_rollout_objects())
        if converged:
            print("[ok] mock sink revision and Service Endpoints converged")
            return
        if time.monotonic() >= deadline:
            fail(f"mock sink rollout did not converge: {summary}")
        time.sleep(poll_seconds)


def wait_all_workloads() -> None:
    for resource in (
        "deployment/hooklane-api",
        "deployment/hooklane-worker",
        "deployment/hooklane-mock-sink",
        "statefulset/hooklane-redis",
        "deployment/hooklane-prometheus",
        "deployment/hooklane-grafana",
    ):
        wait_rollout(resource)
    print("[ok] application and observability workloads are Ready")


def deployment_template_image(resource: dict[str, Any], label: str) -> str:
    spec = resource.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    template_spec = template.get("spec") if isinstance(template, dict) else None
    containers = template_spec.get("containers") if isinstance(template_spec, dict) else None
    if (
        not isinstance(containers, list)
        or len(containers) != 1
        or not isinstance(containers[0], dict)
        or not isinstance(containers[0].get("image"), str)
    ):
        fail(f"{label} Pod template image is malformed")
    return cast(str, containers[0]["image"])


def verify_application_image_tags(image_tag: str) -> None:
    expected_images = dict(
        zip(
            ("api", "worker", "mock-sink"),
            kind_runtime.application_images(image_tag),
            strict=True,
        )
    )
    checked_revisions = 0
    for component, deployment_name in (
        ("api", "hooklane-api"),
        ("worker", "hooklane-worker"),
        ("mock-sink", MOCK_SINK_DEPLOYMENT),
    ):
        selector = (
            f"app.kubernetes.io/instance={kind_runtime.RELEASE},"
            f"app.kubernetes.io/component={component}"
        )
        deployment = kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "deployment",
            deployment_name,
        )
        replica_sets = object_items(
            kubectl_json(
                "--namespace",
                kind_runtime.NAMESPACE,
                "get",
                "replicasets",
                "--selector",
                selector,
            ),
            f"{component} ReplicaSet",
        )
        expected_image = expected_images[component]
        resources = [("Deployment", deployment), *[("ReplicaSet", item) for item in replica_sets]]
        for resource_kind, resource in resources:
            actual_image = deployment_template_image(
                resource,
                f"{component} {resource_kind}",
            )
            if actual_image != expected_image:
                fail(
                    f"{component} {resource_kind} uses {actual_image}; "
                    f"expected {expected_image}"
                )
            checked_revisions += 1
    print(f"[ok] {checked_revisions} application Deployment revisions use {image_tag}")


def e2e_helm_arguments(image_tag: str, *arguments: str) -> tuple[str, ...]:
    return (*arguments, *kind_runtime.image_overrides(image_tag))


def deploy_e2e_release(image_tag: str | None = None) -> None:
    resolved_image_tag = image_tag if image_tag is not None else kind_runtime.resolve_image_tag()
    kind_runtime.helm(
        *e2e_helm_arguments(
            resolved_image_tag,
            "upgrade",
            "--install",
            kind_runtime.RELEASE,
            str(kind_runtime.CHART),
            "--namespace",
            kind_runtime.NAMESPACE,
            "--create-namespace",
            "--set",
            "observability.enabled=true",
            "--set",
            "retry.maximumAttempts=20",
            "--set",
            "retry.pendingIdleMilliseconds=1000",
            "--wait",
            "--timeout",
            "240s",
            "--history-max",
            "5",
        ),
    )
    wait_all_workloads()
    verify_application_image_tags(resolved_image_tag)


def configure_sink_helm(
    mode: str,
    delay_seconds: int,
    image_tag: str | None = None,
) -> None:
    resolved_image_tag = image_tag if image_tag is not None else kind_runtime.resolve_image_tag()
    kind_runtime.helm(
        *e2e_helm_arguments(
            resolved_image_tag,
            "upgrade",
            kind_runtime.RELEASE,
            str(kind_runtime.CHART),
            "--namespace",
            kind_runtime.NAMESPACE,
            "--reuse-values",
            "--set",
            f"mockSink.failureMode={mode}",
            "--set",
            f"mockSink.delaySeconds={delay_seconds}",
            "--wait",
            "--timeout",
            "180s",
            "--history-max",
            "5",
        ),
    )
    wait_rollout(f"deployment/{MOCK_SINK_DEPLOYMENT}")
    wait_for_mock_sink_rollout()
    verify_application_image_tags(resolved_image_tag)


def restore_normal_release(image_tag: str | None = None) -> None:
    resolved_image_tag = image_tag if image_tag is not None else kind_runtime.resolve_image_tag()
    kind_runtime.helm(
        *e2e_helm_arguments(
            resolved_image_tag,
            "upgrade",
            kind_runtime.RELEASE,
            str(kind_runtime.CHART),
            "--namespace",
            kind_runtime.NAMESPACE,
            "--set",
            "observability.enabled=true",
            "--set",
            "mockSink.failureMode=accept",
            "--set",
            "mockSink.delaySeconds=0",
            "--set",
            "retry.maximumAttempts=5",
            "--set",
            "retry.pendingIdleMilliseconds=60000",
            "--wait",
            "--timeout",
            "240s",
            "--history-max",
            "5",
        ),
    )
    wait_all_workloads()
    wait_for_mock_sink_rollout()
    verify_application_image_tags(resolved_image_tag)


def verify_normal_and_idempotent_delivery() -> list[str]:
    normal_id = post_event("kind.e2e.normal")
    normal = wait_event_state(normal_id, "delivered")
    if normal.get("attempt_count") != 1:
        fail("normal delivery did not complete in one attempt")

    idempotency_marker = "kind-e2e-idempotency"
    first_id = post_event(
        "kind.e2e.idempotent", idempotency_key=idempotency_marker
    )
    second_id = post_event(
        "kind.e2e.idempotent", idempotency_key=idempotency_marker
    )
    if first_id != second_id:
        fail("idempotent requests returned different event IDs")
    idempotent = wait_event_state(first_id, "delivered")
    if idempotent.get("attempt_count") != 1:
        fail("idempotent request produced an extra queue delivery")
    print("[ok] normal 202 delivery, status API, and idempotency passed")
    return [normal_id, first_id]


def verify_retry_recovery(image_tag: str) -> str:
    configure_sink_helm("server_error", 0, image_tag)
    event_id = post_event("kind.e2e.retry")
    wait_event_state(event_id, "retry_scheduled", timeout_seconds=45)
    verify_metrics_after_retry()
    configure_sink_helm("accept", 0, image_tag)
    delivered = wait_event_state(event_id, "delivered", timeout_seconds=120)
    attempt_count = delivered.get("attempt_count")
    if not isinstance(attempt_count, int) or attempt_count < 2:
        fail("retry recovery did not record multiple attempts")
    print("[ok] retryable 5xx was scheduled and delivered after sink recovery")
    return event_id


def worker_pod_name() -> str:
    pods = kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "pods",
        "--selector",
        "app.kubernetes.io/component=worker",
    )
    names: list[str] = []
    for item in cast(list[dict[str, Any]], pods.get("items", [])):
        metadata = cast(dict[str, Any], item.get("metadata", {}))
        name = metadata.get("name")
        if isinstance(name, str) and metadata.get("deletionTimestamp") is None:
            names.append(name)
    if len(names) != 1:
        fail("expected exactly one active worker Pod")
    return names[0]


def wait_for_no_worker_pods() -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pods = kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "pods",
            "--selector",
            "app.kubernetes.io/component=worker",
        )
        items = cast(list[dict[str, Any]], pods.get("items", []))
        active = [
            item
            for item in items
            if cast(dict[str, Any], item.get("metadata", {})).get("deletionTimestamp") is None
        ]
        if not active:
            return
        time.sleep(0.2)
    fail("worker Pod did not stop")


def verify_pending_recovery(image_tag: str) -> str:
    configure_sink_helm("delay", 8, image_tag)
    original_worker = worker_pod_name()
    event_id = post_event("kind.e2e.pending-recovery")
    wait_event_state(event_id, "delivering", timeout_seconds=45)

    run_kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "scale",
        "deployment/hooklane-worker",
        "--replicas=0",
    )
    run_kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "delete",
        "pod",
        original_worker,
        "--grace-period=0",
        "--force",
        "--wait=false",
    )
    wait_for_no_worker_pods()

    run_kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "set",
        "env",
        "deployment/hooklane-mock-sink",
        "HOOKLANE_MOCK_SINK_MODE=accept",
        "HOOKLANE_MOCK_SINK_DELAY_SECONDS=0",
    )
    wait_rollout(f"deployment/{MOCK_SINK_DEPLOYMENT}")
    wait_for_mock_sink_rollout()
    verify_application_image_tags(image_tag)
    run_kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "scale",
        "deployment/hooklane-worker",
        "--replicas=1",
    )
    wait_rollout("deployment/hooklane-worker")
    replacement_worker = worker_pod_name()
    if replacement_worker == original_worker:
        fail("worker Pod was not replaced")

    delivered = wait_event_state(event_id, "delivered", timeout_seconds=90)
    attempt_count = delivered.get("attempt_count")
    if not isinstance(attempt_count, int) or attempt_count < 2:
        fail("pending recovery did not redeliver the unacknowledged event")
    print("[ok] forced worker stop left a pending message that replacement recovered")
    return event_id


def prometheus_value(expression: str) -> float:
    response = observability_runtime.prometheus_query(expression)
    data = response.get("data")
    if not isinstance(data, dict):
        fail("Prometheus response has no data")
    result = data.get("result")
    if not isinstance(result, list) or not result:
        fail("Prometheus query returned no series")
    first = result[0]
    if not isinstance(first, dict):
        fail("Prometheus result is not an object")
    value = first.get("value")
    if not isinstance(value, list) or len(value) != 2:
        fail("Prometheus result has no scalar value")
    return float(value[1])


def wait_metric(expression: str, predicate: Callable[[float], bool], label: str) -> float:
    deadline = time.monotonic() + 75
    last_value = -1.0
    while time.monotonic() < deadline:
        try:
            last_value = prometheus_value(expression)
        except RuntimeError:
            time.sleep(2)
            continue
        if predicate(last_value):
            return last_value
        time.sleep(2)
    fail(f"Prometheus metric did not satisfy {label}; last value was {last_value}")


def verify_metrics_after_retry() -> None:
    wait_metric(
        'sum(hooklane_retry_scheduled_total{service="worker"})',
        lambda value: value >= 1,
        "retry scheduled >= 1",
    )
    print("[ok] retry metric increased")


def verify_final_state(event_ids: list[str]) -> None:
    for event_id in event_ids:
        record = event_status(event_id)
        if record.get("status") != "delivered":
            fail("an accepted E2E event was not delivered")
    observability_runtime.wait_for_targets()
    wait_metric(
        'sum(hooklane_delivery_outcomes_total{service="worker",outcome="success"})',
        lambda value: value >= 1,
        "delivery success >= 1",
    )
    wait_metric(
        'sum(hooklane_queue_depth{service="api"})',
        lambda value: value == 0,
        "queue depth = 0",
    )
    wait_metric(
        'sum(hooklane_pending_messages{service="worker"})',
        lambda value: value == 0,
        "pending messages = 0",
    )
    print("[ok] all event states, Prometheus targets, queue depth, and pending metrics passed")


def sanitize_diagnostics(text: str) -> str:
    lines = []
    for line in text.splitlines():
        lines.append("[redacted diagnostic line]" if DIAGNOSTIC_FORBIDDEN.search(line) else line)
    return "\n".join(lines) + "\n"


def diagnostic_command(arguments: list[str]) -> str:
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = f"exit_code={completed.returncode}\n{completed.stdout}\n{completed.stderr}"
    return sanitize_diagnostics(combined)


def diagnostic_prometheus_targets() -> str:
    try:
        with observability_runtime.port_forward(
            "hooklane-prometheus",
            observability_runtime.PROMETHEUS_LOCAL_PORT,
            9090,
        ):
            observability_runtime.wait_json(
                "http://127.0.0.1:19090/api/v1/status/runtimeinfo",
                timeout_seconds=30,
            )
            targets = observability_runtime.request_json(
                "http://127.0.0.1:19090/api/v1/targets"
            )
        data = targets.get("data")
        active = data.get("activeTargets", []) if isinstance(data, dict) else []
        counts: dict[str, dict[str, int]] = {}
        for raw_target in active if isinstance(active, list) else []:
            if not isinstance(raw_target, dict):
                continue
            labels = raw_target.get("labels")
            component = labels.get("component") if isinstance(labels, dict) else None
            health = raw_target.get("health")
            if not isinstance(component, str) or not isinstance(health, str):
                continue
            component_counts = counts.setdefault(component, {})
            component_counts[health] = component_counts.get(health, 0) + 1
        return json.dumps(counts, sort_keys=True, indent=2) + "\n"
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return "Prometheus target summary unavailable\n"


def write_diagnostics() -> None:
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    directory = artifacts / f"kind-e2e-{int(time.time())}-{os.getpid()}"
    directory.mkdir()
    commands = {
        "get-all.txt": kubectl_command(
            "--namespace", kind_runtime.NAMESPACE, "get", "all", "-o", "wide"
        ),
        "events.txt": kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "events",
            "--sort-by=.lastTimestamp",
        ),
        "describe-deployments.txt": kubectl_command(
            "--namespace", kind_runtime.NAMESPACE, "describe", "deployments"
        ),
        "describe-statefulsets.txt": kubectl_command(
            "--namespace", kind_runtime.NAMESPACE, "describe", "statefulsets"
        ),
        "application-logs.txt": kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "logs",
            "--selector",
            f"app.kubernetes.io/instance={kind_runtime.RELEASE}",
            "--all-containers=true",
            "--tail=100",
            "--prefix=true",
        ),
        "helm-status.txt": [
            "helm",
            "--kubeconfig",
            str(kind_runtime.KUBECONFIG),
            "--kube-context",
            kind_runtime.CONTEXT_NAME,
            "status",
            kind_runtime.RELEASE,
            "--namespace",
            kind_runtime.NAMESPACE,
        ],
        "helm-values.txt": [
            "helm",
            "--kubeconfig",
            str(kind_runtime.KUBECONFIG),
            "--kube-context",
            kind_runtime.CONTEXT_NAME,
            "get",
            "values",
            kind_runtime.RELEASE,
            "--namespace",
            kind_runtime.NAMESPACE,
            "--all",
        ],
    }
    for filename, command in commands.items():
        (directory / filename).write_text(diagnostic_command(command), encoding="utf-8")
    (directory / "prometheus-targets.json").write_text(
        diagnostic_prometheus_targets(),
        encoding="utf-8",
    )
    (directory / "README.txt").write_text(
        "Sanitized Hooklane kind E2E diagnostics. Sensitive request and connection "
        "content is omitted.\n",
        encoding="utf-8",
    )
    print(f"[diagnostics] sanitized files written under {directory.relative_to(ROOT)}")


def release_exists() -> bool:
    result = kind_runtime.helm(
        "status",
        kind_runtime.RELEASE,
        "--namespace",
        kind_runtime.NAMESPACE,
        check=False,
    )
    return result.returncode == 0


def run_e2e(image_tag: str) -> None:
    kind_runtime.deploy(image_tag)
    deploy_e2e_release(image_tag)
    kind_runtime.helm_test()
    wait_api_ready()
    with observability_runtime.port_forward(
        "hooklane-prometheus",
        observability_runtime.PROMETHEUS_LOCAL_PORT,
        9090,
    ):
        observability_runtime.wait_json(
            "http://127.0.0.1:19090/api/v1/status/runtimeinfo",
            timeout_seconds=60,
        )
        observability_runtime.wait_for_targets()
        event_ids = verify_normal_and_idempotent_delivery()
        retry_id = verify_retry_recovery(image_tag)
        pending_id = verify_pending_recovery(image_tag)
        event_ids.extend((retry_id, pending_id))
        verify_final_state(event_ids)


def main() -> int:
    owned_cluster = kind_runtime.CLUSTER_NAME not in kind_runtime.clusters()
    passed = True
    image_tag: str | None = None
    try:
        image_tag = kind_runtime.resolve_image_tag()
        if owned_cluster:
            kind_runtime.cluster_up()
        run_e2e(image_tag)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        passed = False
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"[fail] kind E2E: {detail}")
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            write_diagnostics()
    finally:
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters() and release_exists():
            try:
                if image_tag is not None:
                    restore_normal_release(image_tag)
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] kind E2E normal configuration restoration failed")
        if owned_cluster:
            try:
                kind_runtime.cluster_down()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] kind E2E cluster cleanup failed")
    if passed:
        print("[ok] kind E2E completed with no accepted event loss")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
