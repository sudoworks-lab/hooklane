"""Verify an available rollout and recovery from an intentionally bad release."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Never, cast

import kind_e2e
import kind_runtime
from chart_smoke import request_json, wait_ready
from kubernetes_resiliency import (
    verify_worker_shutdown_and_recovery,
    wait_event_state,
    wait_for_deployment,
)


ROOT = Path(__file__).resolve().parents[1]
API_REPLICAS = 2
NORMAL_ROLLOUT_REQUEST_SAMPLES = 20
NORMAL_ROLLOUT_MAX_TRANSIENT_FAILURES = 1
BAD_READINESS_PATH = "/hooklane-intentionally-not-ready"
NORMAL_READINESS_PATH = "/health/ready"


def fail(message: str) -> Never:
    raise RuntimeError(message)


def helm_command(*arguments: str) -> list[str]:
    return [
        "helm",
        "--kubeconfig",
        str(kind_runtime.KUBECONFIG),
        "--kube-context",
        kind_runtime.CONTEXT_NAME,
        *arguments,
    ]


def kubectl_command(*arguments: str) -> list[str]:
    return [
        "kubectl",
        "--kubeconfig",
        str(kind_runtime.KUBECONFIG),
        "--context",
        kind_runtime.CONTEXT_NAME,
        *arguments,
    ]


def captured(
    command: list[str],
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def kubectl_json(*arguments: str) -> dict[str, Any]:
    completed = captured(kubectl_command(*arguments, "-o", "json"))
    if completed.returncode != 0:
        fail("kubectl JSON command failed")
    parsed: object = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        fail("kubectl response is not a JSON object")
    return cast(dict[str, Any], parsed)


def helm_history() -> list[dict[str, Any]]:
    completed = captured(
        helm_command(
            "history",
            kind_runtime.RELEASE,
            "--namespace",
            kind_runtime.NAMESPACE,
            "--output",
            "json",
        )
    )
    if completed.returncode != 0:
        fail("Helm release history could not be read")
    parsed: object = json.loads(completed.stdout)
    if not isinstance(parsed, list) or not parsed:
        fail("Helm release history is empty or malformed")
    if not all(isinstance(entry, dict) for entry in parsed):
        fail("Helm release history contains a malformed entry")
    return cast(list[dict[str, Any]], parsed)


def revision_number(entry: dict[str, Any]) -> int:
    value = entry.get("revision")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    fail("Helm release history has no numeric revision")


def deployed_revision() -> int:
    current = helm_history()[-1]
    if current.get("status") != "deployed":
        fail("current Helm release is not deployed")
    return revision_number(current)


def deployment_status() -> tuple[dict[str, Any], dict[str, Any]]:
    deployment = kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "deployment",
        "hooklane-api",
    )
    spec = deployment.get("spec")
    status = deployment.get("status")
    if not isinstance(spec, dict) or not isinstance(status, dict):
        fail("API Deployment has no spec or status")
    return cast(dict[str, Any], spec), cast(dict[str, Any], status)


def readiness_path() -> str:
    spec, _status = deployment_status()
    template = spec.get("template")
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    if not isinstance(containers, list) or not containers:
        fail("API Deployment has no container")
    container = containers[0]
    probe = container.get("readinessProbe") if isinstance(container, dict) else None
    http_get = probe.get("httpGet") if isinstance(probe, dict) else None
    path = http_get.get("path") if isinstance(http_get, dict) else None
    if not isinstance(path, str):
        fail("API Deployment has no HTTP readiness path")
    return path


def api_pod_counts() -> tuple[int, int, set[str]]:
    pods = kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "pods",
        "--selector",
        "app.kubernetes.io/component=api",
    )
    items = pods.get("items")
    if not isinstance(items, list):
        fail("API Pod list is malformed")
    ready_count = 0
    not_ready_count = 0
    template_hashes: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            fail("API Pod entry is malformed")
        metadata = item.get("metadata")
        status = item.get("status")
        if not isinstance(metadata, dict) or not isinstance(status, dict):
            fail("API Pod has no metadata or status")
        labels = metadata.get("labels")
        template_hash = labels.get("pod-template-hash") if isinstance(labels, dict) else None
        if isinstance(template_hash, str):
            template_hashes.add(template_hash)
        conditions = status.get("conditions")
        is_ready = False
        if isinstance(conditions, list):
            is_ready = any(
                isinstance(condition, dict)
                and condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in conditions
            )
        if metadata.get("deletionTimestamp") is None and is_ready:
            ready_count += 1
        elif metadata.get("deletionTimestamp") is None:
            not_ready_count += 1
    return ready_count, not_ready_count, template_hashes


def api_request_succeeded() -> bool:
    try:
        status, _response = request_json("GET", "/health/ready")
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return status == 200


def verify_rollout_contract() -> None:
    spec, status = deployment_status()
    strategy = spec.get("strategy")
    rolling = strategy.get("rollingUpdate") if isinstance(strategy, dict) else None
    if spec.get("replicas") != API_REPLICAS:
        fail("API rollout requires exactly two replicas")
    if not isinstance(rolling, dict):
        fail("API Deployment does not use RollingUpdate")
    if rolling.get("maxUnavailable") != 0 or rolling.get("maxSurge") != 1:
        fail("API rolling strategy is not maxUnavailable 0 and maxSurge 1")
    if status.get("availableReplicas") != API_REPLICAS:
        fail("API does not start with two available replicas")
    if readiness_path() != NORMAL_READINESS_PATH:
        fail("API does not start with the normal readiness path")
    print("[ok] API has two replicas, readiness gating, and 0/1 rolling strategy")


def rollout_complete(deployment: dict[str, Any], spec: dict[str, Any], status: dict[str, Any]) -> bool:
    metadata = deployment.get("metadata")
    generation = metadata.get("generation") if isinstance(metadata, dict) else None
    desired = spec.get("replicas")
    return (
        isinstance(desired, int)
        and status.get("observedGeneration") == generation
        and status.get("updatedReplicas") == desired
        and status.get("availableReplicas") == desired
        and int(status.get("unavailableReplicas", 0)) == 0
    )


def verify_normal_rollout() -> None:
    _initial_ready, _initial_not_ready, initial_hashes = api_pod_counts()
    if len(initial_hashes) != 1:
        fail("normal API rollout did not start from one Pod template")
    patch = json.dumps(
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "hooklane.test/rollout-smoke": str(time.time_ns())
                        }
                    }
                }
            }
        }
    )
    subprocess.run(
        kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "patch",
            "deployment",
            "hooklane-api",
            "--type=merge",
            "--patch",
            patch,
        ),
        cwd=ROOT,
        check=True,
    )
    deadline = time.monotonic() + 180
    minimum_available = API_REPLICAS
    request_attempts = 0
    request_failures = 0
    consecutive_request_failures = 0
    maximum_consecutive_request_failures = 0
    saw_old_and_new_pods = False
    saw_new_template = False
    while time.monotonic() < deadline:
        request_attempts += 1
        if not api_request_succeeded():
            request_failures += 1
            consecutive_request_failures += 1
            maximum_consecutive_request_failures = max(
                maximum_consecutive_request_failures,
                consecutive_request_failures,
            )
        else:
            consecutive_request_failures = 0
        deployment = kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "deployment",
            "hooklane-api",
        )
        spec = deployment.get("spec")
        status = deployment.get("status")
        if not isinstance(spec, dict) or not isinstance(status, dict):
            fail("API rollout status is malformed")
        available = int(status.get("availableReplicas", 0))
        minimum_available = min(minimum_available, available)
        _ready, _not_ready, hashes = api_pod_counts()
        saw_old_and_new_pods = saw_old_and_new_pods or len(hashes) >= 2
        saw_new_template = saw_new_template or bool(hashes - initial_hashes)
        if rollout_complete(
            deployment,
            cast(dict[str, Any], spec),
            cast(dict[str, Any], status),
        ) and request_attempts >= NORMAL_ROLLOUT_REQUEST_SAMPLES:
            break
        time.sleep(0.1)
    else:
        fail("normal API rollout did not complete")
    history = captured(
        kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "rollout",
            "history",
            "deployment/hooklane-api",
        )
    )
    if history.returncode != 0 or "REVISION" not in history.stdout:
        fail("Kubernetes rollout history is unavailable")
    if minimum_available < API_REPLICAS:
        fail("normal API rollout dropped below two available replicas")
    if request_attempts < NORMAL_ROLLOUT_REQUEST_SAMPLES:
        fail("normal API rollout request sampling was incomplete")
    if (
        request_failures > NORMAL_ROLLOUT_MAX_TRANSIENT_FAILURES
        or maximum_consecutive_request_failures > 1
    ):
        fail(
            "normal API rollout had sustained readiness failures "
            f"(failures={request_failures}, attempts={request_attempts}, "
            f"maximum_consecutive={maximum_consecutive_request_failures})"
        )
    if not saw_old_and_new_pods and not saw_new_template:
        fail("normal API rollout did not expose or complete a Pod template transition")
    print(
        "[ok] normal rollout kept two available APIs without sustained request "
        f"interruption ({request_attempts} checks, {request_failures} transient failures)"
    )


def verify_delivery(event_type: str) -> str:
    status, response = request_json(
        "POST",
        "/v1/events",
        {"event_type": event_type, "payload": {}},
    )
    event_id = response.get("event_id")
    if status != 202 or not isinstance(event_id, str):
        fail("rollout smoke event was not accepted")
    delivered = wait_event_state(event_id, "delivered", 60)
    if delivered.get("attempt_count") != 1:
        fail("rollout smoke event was not delivered exactly once")
    return event_id


def verify_bad_release(previous_revision: int) -> int:
    process = subprocess.Popen(
        helm_command(
            "upgrade",
            kind_runtime.RELEASE,
            str(kind_runtime.CHART),
            "--namespace",
            kind_runtime.NAMESPACE,
            "--reuse-values",
            "--set",
            f"api.probes.readiness.path={BAD_READINESS_PATH}",
            "--wait",
            "--timeout",
            "20s",
            "--history-max",
            "5",
        ),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    minimum_available = API_REPLICAS
    request_attempts = 0
    request_failures = 0
    try:
        while process.poll() is None:
            request_attempts += 1
            if not api_request_succeeded():
                request_failures += 1
            _spec, status = deployment_status()
            minimum_available = min(
                minimum_available,
                int(status.get("availableReplicas", 0)),
            )
            time.sleep(0.2)
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)
    if process.returncode == 0:
        fail("intentionally bad Helm release returned false success")
    history = helm_history()
    failed = history[-1]
    bad_revision = revision_number(failed)
    if bad_revision <= previous_revision or failed.get("status") != "failed":
        fail("intentionally bad Helm release was not recorded as failed")
    if readiness_path() != BAD_READINESS_PATH:
        fail("intentionally bad readiness configuration was not rendered")
    ready_count, not_ready_count, hashes = api_pod_counts()
    if ready_count < API_REPLICAS or not_ready_count < 1 or len(hashes) < 2:
        fail("bad release did not retain old Ready Pods beside a NotReady Pod")
    rollout_status = captured(
        kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "rollout",
            "status",
            "deployment/hooklane-api",
            "--timeout=2s",
        ),
        timeout=10,
    )
    if rollout_status.returncode == 0:
        fail("bad Deployment rollout returned false success")
    if minimum_available < API_REPLICAS:
        fail("bad release dropped below two available API replicas")
    if request_attempts < 10 or request_failures != 0:
        fail("bad release interrupted traffic through the retained Ready Pods")
    print(
        "[ok] bad release failed closed with two Ready old Pods, one NotReady new Pod, "
        f"and {request_attempts} successful request checks"
    )
    print(
        f"[ok] failure diagnostics captured Helm revision {bad_revision}, "
        f"Ready={ready_count}, NotReady={not_ready_count}, rollout status nonzero"
    )
    return bad_revision


def wait_application_ready() -> None:
    for name in ("hooklane-api", "hooklane-worker", "hooklane-mock-sink"):
        wait_for_deployment(name)
    subprocess.run(
        kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "rollout",
            "status",
            "statefulset/hooklane-redis",
            "--timeout=180s",
        ),
        cwd=ROOT,
        check=True,
    )
    wait_ready()


def rollback_release(previous_revision: int, bad_revision: int) -> None:
    completed = captured(
        helm_command(
            "rollback",
            kind_runtime.RELEASE,
            str(previous_revision),
            "--namespace",
            kind_runtime.NAMESPACE,
            "--wait",
            "--timeout",
            "180s",
        ),
        timeout=210,
    )
    if completed.returncode != 0:
        fail("Helm rollback failed")
    wait_application_ready()
    history = helm_history()
    current = history[-1]
    current_revision = revision_number(current)
    description = current.get("description")
    if current.get("status") != "deployed" or current_revision <= bad_revision:
        fail("rollback did not create a deployed recovery revision")
    if not isinstance(description, str) or "rollback" not in description.lower():
        fail("Helm history does not identify the recovery as a rollback")
    if readiness_path() != NORMAL_READINESS_PATH:
        fail("bad readiness configuration remained after rollback")
    ready_count, not_ready_count, _hashes = api_pod_counts()
    if ready_count != API_REPLICAS or not_ready_count != 0:
        fail("API Pods did not settle after rollback")
    verify_delivery("kind.rollout.recovery")
    rollout_history = captured(
        kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "rollout",
            "history",
            "deployment/hooklane-api",
        )
    )
    if rollout_history.returncode != 0 or "REVISION" not in rollout_history.stdout:
        fail("rollout history is unavailable after rollback")
    print(
        f"[ok] Helm rollback restored revision {previous_revision} as deployed "
        f"revision {current_revision}; recovery delivery passed"
    )


def main() -> int:
    kind_runtime.require_cluster()
    wait_application_ready()
    verify_rollout_contract()
    verify_delivery("kind.rollout.baseline")
    verify_normal_rollout()
    graceful_event_id = verify_worker_shutdown_and_recovery()
    if wait_event_state(graceful_event_id, "delivered", 30).get("attempt_count") != 1:
        fail("graceful worker update lost or duplicated the in-flight event")
    previous_revision = deployed_revision()
    bad_started = True
    recovered = False
    bad_revision = previous_revision
    try:
        bad_revision = verify_bad_release(previous_revision)
        rollback_release(previous_revision, bad_revision)
        recovered = True
    except (
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            kind_e2e.write_diagnostics()
        raise
    finally:
        if bad_started and not recovered:
            emergency = captured(
                helm_command(
                    "rollback",
                    kind_runtime.RELEASE,
                    str(previous_revision),
                    "--namespace",
                    kind_runtime.NAMESPACE,
                    "--wait",
                    "--timeout",
                    "180s",
                ),
                timeout=210,
            )
            if emergency.returncode != 0:
                print("[fail] emergency rollback did not complete")
    print("[ok] rolling update, graceful worker drain, bad release, rollback, and recovery passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"[fail] rollout smoke: {detail}")
        raise SystemExit(1) from None
