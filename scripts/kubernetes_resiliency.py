"""Exercise local Kubernetes rollout, readiness, shutdown, and recovery contracts."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Never, cast

from chart_smoke import request_json, wait_ready
from kind_runtime import (
    CHART,
    CONTEXT_NAME,
    KUBECONFIG,
    NAMESPACE,
    RELEASE,
    application_images,
    require_cluster,
    resolve_image_tag,
)


API_REPLICAS = 2


def fail(message: str) -> Never:
    raise RuntimeError(message)


def kubectl_command(*args: str) -> list[str]:
    return [
        "kubectl",
        "--kubeconfig",
        str(KUBECONFIG),
        "--context",
        CONTEXT_NAME,
        *args,
    ]


def kubectl_json(*args: str) -> dict[str, Any]:
    completed = subprocess.run(
        kubectl_command(*args, "-o", "json"),
        check=True,
        capture_output=True,
        text=True,
    )
    parsed: object = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        fail("kubectl JSON response is not an object")
    return cast(dict[str, Any], parsed)


def run_kubectl(*args: str) -> None:
    subprocess.run(kubectl_command(*args), check=True)


def run_helm(*args: str) -> None:
    subprocess.run(
        [
            "helm",
            "--kubeconfig",
            str(KUBECONFIG),
            "--kube-context",
            CONTEXT_NAME,
            *args,
        ],
        check=True,
    )


def wait_for_deployment(name: str) -> None:
    run_kubectl(
        "--namespace",
        NAMESPACE,
        "rollout",
        "status",
        f"deployment/{name}",
        "--timeout=180s",
    )


def wait_for_single_ready_component(component: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pods = kubectl_json(
            "--namespace",
            NAMESPACE,
            "get",
            "pods",
            "--selector",
            f"app.kubernetes.io/component={component}",
        )
        items = cast(list[dict[str, Any]], pods.get("items", []))
        if len(items) == 1:
            metadata = cast(dict[str, Any], items[0].get("metadata", {}))
            status = cast(dict[str, Any], items[0].get("status", {}))
            conditions = cast(list[dict[str, Any]], status.get("conditions", []))
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in conditions
            )
            if metadata.get("deletionTimestamp") is None and ready:
                return
        time.sleep(0.2)
    fail(f"{component} rollout did not settle on one Ready Pod")


def verify_api_rollout_availability() -> None:
    deployment = kubectl_json("--namespace", NAMESPACE, "get", "deployment", "hooklane-api")
    if deployment.get("status", {}).get("availableReplicas") != API_REPLICAS:
        fail("API does not start with two available replicas")
    patch = json.dumps(
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"hooklane.test/rollout": str(time.time_ns())}
                    }
                }
            }
        }
    )
    run_kubectl(
        "--namespace",
        NAMESPACE,
        "patch",
        "deployment",
        "hooklane-api",
        "--type=merge",
        "--patch",
        patch,
    )
    deadline = time.monotonic() + 180
    minimum_available = API_REPLICAS
    while time.monotonic() < deadline:
        current = kubectl_json("--namespace", NAMESPACE, "get", "deployment", "hooklane-api")
        spec = cast(dict[str, Any], current.get("spec", {}))
        status = cast(dict[str, Any], current.get("status", {}))
        available = int(status.get("availableReplicas", 0))
        minimum_available = min(minimum_available, available)
        desired = int(spec.get("replicas", 0))
        if (
            status.get("observedGeneration") == current.get("metadata", {}).get("generation")
            and status.get("updatedReplicas") == desired
            and available == desired
            and int(status.get("unavailableReplicas", 0)) == 0
        ):
            if minimum_available < API_REPLICAS:
                fail("API rollout dropped below two available replicas")
            print("[ok] API rollout kept two available replicas")
            return
        time.sleep(0.2)
    fail("API rollout did not complete")


def create_unready_api_pod() -> str:
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "hooklane-unready-probe-test",
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "hooklane",
                "app.kubernetes.io/instance": RELEASE,
                "app.kubernetes.io/component": "api",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "serviceAccountName": "hooklane",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "api",
                    "image": application_images(resolve_image_tag())[0],
                    "imagePullPolicy": "IfNotPresent",
                    "envFrom": [{"configMapRef": {"name": "hooklane-config"}}],
                    "ports": [{"name": "http", "containerPort": 8080}],
                    "readinessProbe": {
                        "exec": {"command": ["python", "-c", "raise SystemExit(1)"]},
                        "periodSeconds": 1,
                        "failureThreshold": 1,
                    },
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "25m", "memory": "32Mi"},
                        "limits": {"cpu": "100m", "memory": "128Mi"},
                    },
                    "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                }
            ],
            "volumes": [
                {
                    "name": "tmp",
                    "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"},
                }
            ],
        },
    }
    subprocess.run(
        kubectl_command("create", "-f", "-"),
        input=json.dumps(manifest),
        check=True,
        text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pod = kubectl_json(
            "--namespace",
            NAMESPACE,
            "get",
            "pod",
            "hooklane-unready-probe-test",
        )
        status = cast(dict[str, Any], pod.get("status", {}))
        conditions = cast(list[dict[str, Any]], status.get("conditions", []))
        ready = next(
            (condition.get("status") for condition in conditions if condition.get("type") == "Ready"),
            None,
        )
        pod_ip = status.get("podIP")
        if status.get("phase") == "Running" and ready == "False" and isinstance(pod_ip, str):
            return pod_ip
        time.sleep(0.2)
    fail("intentionally unready API Pod did not reach Running/NotReady")


def verify_not_ready_endpoint_exclusion() -> None:
    pod_ip = create_unready_api_pod()
    try:
        slices = kubectl_json(
            "--namespace",
            NAMESPACE,
            "get",
            "endpointslices",
            "--selector",
            "kubernetes.io/service-name=hooklane-api",
        )
        ready_addresses: set[str] = set()
        for item in cast(list[dict[str, Any]], slices.get("items", [])):
            for endpoint in cast(list[dict[str, Any]], item.get("endpoints", [])):
                conditions = cast(dict[str, Any], endpoint.get("conditions", {}))
                if conditions.get("ready") is True:
                    ready_addresses.update(cast(list[str], endpoint.get("addresses", [])))
        if pod_ip in ready_addresses:
            fail("NotReady API Pod was included in ready Service endpoints")
        if len(ready_addresses) < API_REPLICAS:
            fail("ready API endpoints dropped below the configured replica count")
        print("[ok] NotReady API Pod was excluded from Service traffic")
    finally:
        run_kubectl(
            "--namespace",
            NAMESPACE,
            "delete",
            "pod",
            "hooklane-unready-probe-test",
            "--wait=true",
            "--timeout=60s",
        )


def worker_pod_name() -> str:
    pods = kubectl_json(
        "--namespace",
        NAMESPACE,
        "get",
        "pods",
        "--selector",
        "app.kubernetes.io/component=worker",
    )
    items = cast(list[dict[str, Any]], pods.get("items", []))
    running = [
        cast(dict[str, Any], item.get("metadata", {})).get("name")
        for item in items
        if cast(dict[str, Any], item.get("status", {})).get("phase") == "Running"
    ]
    names = [name for name in running if isinstance(name, str)]
    if len(names) != 1:
        fail(f"expected one running worker Pod, found {names}")
    return names[0]


def wait_event_state(event_id: str, state: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            status, record = request_json("GET", f"/v1/events/{event_id}")
        except OSError:
            time.sleep(0.1)
            continue
        if status == 200 and record.get("status") == state:
            return record
        time.sleep(0.1)
    fail(f"event {event_id} did not reach {state}")


def post_event(event_type: str) -> str:
    status, response = request_json(
        "POST",
        "/v1/events",
        {"event_type": event_type, "payload": {}},
    )
    event_id = response.get("event_id")
    if status != 202 or not isinstance(event_id, str):
        fail("API did not accept resiliency event")
    return event_id


def configure_mock_sink(mode: str, delay_seconds: int) -> None:
    run_helm(
        "upgrade",
        RELEASE,
        str(CHART),
        "--namespace",
        NAMESPACE,
        "--reuse-values",
        "--set",
        f"mockSink.failureMode={mode}",
        "--set",
        f"mockSink.delaySeconds={delay_seconds}",
        "--wait",
        "--timeout",
        "180s",
    )
    wait_for_deployment("hooklane-mock-sink")
    wait_for_single_ready_component("mock-sink")


def verify_worker_shutdown_and_recovery() -> str:
    configure_mock_sink("delay", 3)
    original_worker = worker_pod_name()
    event_id = post_event("kind.graceful-shutdown")
    wait_event_state(event_id, "delivering", 30)
    run_kubectl(
        "--namespace",
        NAMESPACE,
        "delete",
        "pod",
        original_worker,
        "--wait=true",
        "--timeout=60s",
    )
    delivered = wait_event_state(event_id, "delivered", 30)
    if delivered.get("attempt_count") != 1:
        fail("graceful worker shutdown caused an extra delivery attempt")
    wait_for_deployment("hooklane-worker")
    replacement = worker_pod_name()
    if replacement == original_worker:
        fail("worker Pod was not recreated")
    configure_mock_sink("accept", 0)
    recovery_id = post_event("kind.worker-recovery")
    wait_event_state(recovery_id, "delivered", 30)
    print("[ok] worker SIGTERM drained in-flight delivery and replacement recovered")
    return event_id


def verify_redis_persistence(event_id: str) -> None:
    run_kubectl(
        "--namespace",
        NAMESPACE,
        "delete",
        "pod",
        "hooklane-redis-0",
        "--wait=true",
        "--timeout=60s",
    )
    run_kubectl(
        "--namespace",
        NAMESPACE,
        "wait",
        "--for=condition=Ready",
        "pod/hooklane-redis-0",
        "--timeout=180s",
    )
    wait_ready()
    record = wait_event_state(event_id, "delivered", 30)
    if record.get("event_id") != event_id:
        fail("Redis recreation lost the persisted event state")
    pvc = kubectl_json("--namespace", NAMESPACE, "get", "pvc", "data-hooklane-redis-0")
    if pvc.get("status", {}).get("phase") != "Bound":
        fail("Redis persistent volume claim is not Bound")
    print("[ok] Redis Pod recreation retained state on the Bound PVC")


def main() -> int:
    require_cluster()
    wait_ready()
    verify_api_rollout_availability()
    verify_not_ready_endpoint_exclusion()
    event_id = verify_worker_shutdown_and_recovery()
    verify_redis_persistence(event_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"[fail] Kubernetes resiliency: {exc}")
        raise SystemExit(1) from None
