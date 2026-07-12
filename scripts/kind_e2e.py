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


def deploy_e2e_release() -> None:
    kind_runtime.helm(
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
    )
    wait_all_workloads()


def configure_sink_helm(mode: str, delay_seconds: int) -> None:
    kind_runtime.helm(
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
    )
    wait_rollout("deployment/hooklane-mock-sink")


def restore_normal_release() -> None:
    kind_runtime.helm(
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
    )
    wait_all_workloads()


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


def verify_retry_recovery() -> str:
    configure_sink_helm("server_error", 0)
    event_id = post_event("kind.e2e.retry")
    wait_event_state(event_id, "retry_scheduled", timeout_seconds=45)
    verify_metrics_after_retry()
    configure_sink_helm("accept", 0)
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


def verify_pending_recovery() -> str:
    configure_sink_helm("delay", 8)
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
    wait_rollout("deployment/hooklane-mock-sink")
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


def run_e2e() -> None:
    kind_runtime.deploy()
    deploy_e2e_release()
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
        retry_id = verify_retry_recovery()
        pending_id = verify_pending_recovery()
        event_ids.extend((retry_id, pending_id))
        verify_final_state(event_ids)


def main() -> int:
    owned_cluster = kind_runtime.CLUSTER_NAME not in kind_runtime.clusters()
    passed = True
    try:
        if owned_cluster:
            kind_runtime.cluster_up()
        run_e2e()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        passed = False
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"[fail] kind E2E: {detail}")
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            write_diagnostics()
    finally:
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters() and release_exists():
            try:
                restore_normal_release()
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
