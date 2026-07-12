"""Exercise Redis outage detection, fail-closed acceptance, and PVC recovery."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from typing import Any, Never, cast

import incident_downstream_5xx as common
import kind_e2e
import kind_runtime
import observability_runtime


ROOT = Path(__file__).resolve().parents[1]
INCIDENT_RECORD = ROOT / "docs" / "incidents" / "redis-outage.md"
RUNBOOK = ROOT / "docs" / "runbooks" / "HooklaneRedisOperationFailures.md"
API_REDIS_FAILURES = 'sum(hooklane_redis_operation_failures_total{service="api"})'
WORKER_REDIS_FAILURES = 'sum(hooklane_redis_operation_failures_total{service="worker"})'
REDIS_RECENT_FAILURES = "sum(increase(hooklane_redis_operation_failures_total[30s]))"
FAILED_ENQUEUE = (
    'sum(hooklane_enqueue_total{service="api",outcome="failure",'
    'reason_code="storage_unavailable"})'
)
QUEUE_METRIC = 'max(hooklane_queue_depth{service=~"api|worker"})'
PENDING_METRIC = 'sum(hooklane_pending_messages{service="worker"})'


def fail(message: str) -> Never:
    raise RuntimeError(message)


def api_pod_names() -> list[str]:
    pods = kind_e2e.kubectl_json(
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
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        if isinstance(name, str) and metadata.get("deletionTimestamp") is None:
            names.append(name)
    if len(names) != 2:
        fail("Redis drill requires exactly two API Pods")
    return sorted(names)


def pod_http_request(pod_name: str, method: str, path: str) -> dict[str, Any]:
    code = (
        "import json,sys;from urllib.error import HTTPError;"
        "from urllib.request import Request,urlopen;"
        "method=sys.argv[1];path=sys.argv[2];"
        "data=(b'{\"event_type\":\"incident.redis.outage\",\"payload\":{}}' "
        "if method=='POST' else None);"
        "request=Request('http://127.0.0.1:8080'+path,data=data,"
        "headers={'Content-Type':'application/json'},method=method);"
        "status=0;body={};"
        "\ntry:\n response=urlopen(request,timeout=3);status=response.status;"
        "body=json.loads(response.read())\n"
        "except HTTPError as error:\n status=error.code;body=json.loads(error.read())\n"
        "print(json.dumps({'status':status,'body':body},sort_keys=True))"
    )
    completed = subprocess.run(
        kind_e2e.kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "exec",
            pod_name,
            "--",
            "python",
            "-c",
            code,
            method,
            path,
        ),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    parsed: object = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        fail("Pod-local HTTP result is malformed")
    return cast(dict[str, Any], parsed)


def wait_pod_http_status(
    pod_name: str,
    path: str,
    expected_status: int,
    *,
    timeout_seconds: float = 60,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status: object = None
    while time.monotonic() < deadline:
        try:
            response = pod_http_request(pod_name, "GET", path)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
            time.sleep(1)
            continue
        last_status = response.get("status")
        if last_status == expected_status:
            return
        time.sleep(1)
    fail(f"Pod endpoint {path} did not return {expected_status}; status={last_status}")


def pod_restart_count(pod_name: str) -> int:
    pod = kind_e2e.kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "pod",
        pod_name,
    )
    status = pod.get("status")
    container_statuses = status.get("containerStatuses") if isinstance(status, dict) else None
    if not isinstance(container_statuses, list) or len(container_statuses) != 1:
        fail("Pod container status is unavailable")
    container_status = container_statuses[0]
    count = container_status.get("restartCount") if isinstance(container_status, dict) else None
    if not isinstance(count, int):
        fail("Pod restart count is unavailable")
    return count


def pod_is_ready(pod_name: str) -> bool:
    pod = kind_e2e.kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "pod",
        pod_name,
    )
    status = pod.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    if not isinstance(conditions, list):
        fail("Pod readiness conditions are unavailable")
    return any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )


def wait_pod_readiness(pod_names: list[str], expected: bool) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if all(pod_is_ready(name) is expected for name in pod_names):
            return
        time.sleep(1)
    state = "Ready" if expected else "NotReady"
    fail(f"Pods did not become {state}")


def pod_logs(pod_name: str) -> str:
    completed = subprocess.run(
        kind_e2e.kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "logs",
            pod_name,
            "--all-containers=true",
            "--tail=1000",
        ),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout


def redis_scalar(*arguments: str) -> str:
    completed = subprocess.run(
        kind_e2e.kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "exec",
            "statefulset/hooklane-redis",
            "--",
            "redis-cli",
            "--raw",
            *arguments,
        ),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def redis_counts() -> tuple[int, int]:
    stream_length = int(redis_scalar("XLEN", "hooklane:events"))
    status_count = int(
        redis_scalar(
            "EVAL",
            "return #redis.call('KEYS','hooklane:event:*')",
            "0",
        )
    )
    return stream_length, status_count


def pvc_uid() -> str:
    pvc = kind_e2e.kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "pvc",
        "data-hooklane-redis-0",
    )
    metadata = pvc.get("metadata")
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    if not isinstance(uid, str):
        fail("Redis PVC UID is unavailable")
    return uid


def wait_redis_pod_absent() -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        pods = kind_e2e.kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "pods",
            "--selector",
            "app.kubernetes.io/component=redis",
        )
        if pods.get("items") == []:
            return
        time.sleep(1)
    fail("Redis Pod did not stop")


def scale_redis(replicas: int) -> None:
    kind_e2e.run_kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "scale",
        "statefulset/hooklane-redis",
        f"--replicas={replicas}",
    )
    if replicas == 0:
        wait_redis_pod_absent()
        return
    kind_e2e.run_kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "wait",
        "--for=condition=Ready",
        "pod/hooklane-redis-0",
        "--timeout=180s",
    )


def verify_redis_logs(api_pod: str, worker_pod: str) -> None:
    api_text = pod_logs(api_pod)
    worker_text = pod_logs(worker_pod)
    combined = f"{api_text}\n{worker_text}".lower()
    if any(
        marker in combined
        for marker in (
            "payload",
            "credential",
            "password",
            "private key",
            "redis://",
            "idempotency-key",
            "cookie",
        )
    ):
        fail("Redis outage logs contain a forbidden marker")
    api_records = common.structured_records(api_text)
    worker_records = common.structured_records(worker_text)
    if not any(
        record.get("event") == "redis_operation_failed"
        and record.get("service") == "api"
        and record.get("reason_code") == "redis_error"
        for record in api_records
    ):
        fail("API Redis failure structured log is missing")
    if not any(
        record.get("event") == "redis_operation_failed"
        and record.get("service") == "worker"
        and record.get("reason_code") == "redis_error"
        for record in worker_records
    ):
        fail("worker Redis failure structured log is missing")


def verify_incident_links() -> None:
    record = INCIDENT_RECORD.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    required_sections = (
        "## 再現手順",
        "## 期待する影響",
        "## 期待するmetrics",
        "## 期待するalert",
        "## Structured log",
        "## 初動切り分け",
        "## 暫定対応",
        "## 復旧手順",
        "## 復旧確認",
        "## データ消失の有無",
        "## 再発防止候補",
        "## 制約と未確認事項",
    )
    if any(section not in record for section in required_sections):
        fail("Redis incident record is missing a required section")
    if "../incidents/redis-outage.md" not in runbook:
        fail("Redis Runbook does not link back to the incident drill")
    for reference in (
        "HooklaneRedisOperationFailures",
        "../runbooks/HooklaneRedisOperationFailures.md",
        "../SLO.md#api受付可用性",
        "hooklane_redis_operation_failures_total",
        "Hooklane SLI and Operations",
    ):
        if reference not in record:
            fail("Redis incident record is missing an operational reference")


def run_drill() -> None:
    kind_runtime.deploy()
    kind_e2e.deploy_e2e_release()
    kind_runtime.helm_test()
    kind_e2e.wait_api_ready()
    verify_incident_links()
    api_pods = api_pod_names()
    api_restarts = {name: pod_restart_count(name) for name in api_pods}
    worker_pod = kind_e2e.worker_pod_name()
    worker_restarts = pod_restart_count(worker_pod)
    baseline_id = kind_e2e.post_event("incident.redis.baseline")
    kind_e2e.wait_event_state(baseline_id, "delivered")
    counts_before = redis_counts()
    original_pvc_uid = pvc_uid()
    redis_stopped = False

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
        api_failures_before = observability_runtime.query_value(API_REDIS_FAILURES)
        worker_failures_before = observability_runtime.query_value(WORKER_REDIS_FAILURES)
        enqueue_failures_before = observability_runtime.query_value(FAILED_ENQUEUE)
        try:
            scale_redis(0)
            redis_stopped = True
            time.sleep(2)
            for pod_name in api_pods:
                readiness = pod_http_request(pod_name, "GET", "/health/ready")
                liveness = pod_http_request(pod_name, "GET", "/health/live")
                if readiness.get("status") != 503 or liveness.get("status") != 200:
                    fail("API readiness/liveness semantics failed during Redis outage")
            wait_pod_readiness([*api_pods, worker_pod], False)
            failed_post = pod_http_request(api_pods[0], "POST", "/v1/events")
            failed_body = failed_post.get("body")
            if failed_post.get("status") == 202:
                fail("API returned false 202 while Redis was unavailable")
            if not isinstance(failed_body, dict) or "event_id" in failed_body:
                fail("failed acceptance returned a partial event status")
            print("[ok] API Pods were NotReady, live, and rejected event acceptance", flush=True)

            api_failure_value = common.wait_metric(
                API_REDIS_FAILURES,
                lambda value: value >= api_failures_before + 1,
                "API Redis failures +1",
            )
            print(f"[ok] API Redis failure metric={api_failure_value:g}", flush=True)
            worker_failure_value = common.wait_metric(
                WORKER_REDIS_FAILURES,
                lambda value: value >= worker_failures_before + 1,
                "worker Redis failures +1",
            )
            print(f"[ok] worker Redis failure metric={worker_failure_value:g}", flush=True)
            enqueue_failure_value = common.wait_metric(
                FAILED_ENQUEUE,
                lambda value: value >= enqueue_failures_before + 1,
                "failed enqueue +1",
            )
            print(f"[ok] failed enqueue metric={enqueue_failure_value:g}", flush=True)
            alert_state = observability_runtime.wait_for_alert(
                "HooklaneRedisOperationFailures",
                {"pending", "firing"},
                timeout_seconds=60,
            )
            print(f"[ok] Redis alert state={alert_state}", flush=True)
            verify_redis_logs(api_pods[0], worker_pod)
            print("[ok] API and worker Redis structured logs passed", flush=True)
        finally:
            if redis_stopped:
                scale_redis(1)
                redis_stopped = False

        if pvc_uid() != original_pvc_uid:
            fail("Redis recovery replaced the persistent volume claim")
        counts_after_recovery = redis_counts()
        if counts_after_recovery != counts_before:
            fail("Redis outage created a partial enqueue or lost persisted state")
        for pod_name in api_pods:
            wait_pod_http_status(pod_name, "/health/ready", 200)
            wait_pod_http_status(pod_name, "/health/live", 200)
            if pod_restart_count(pod_name) != api_restarts[pod_name]:
                fail("API Pod restarted during the Redis outage")
        wait_pod_readiness([*api_pods, worker_pod], True)
        kind_e2e.wait_api_ready()
        kind_e2e.wait_rollout("deployment/hooklane-worker")
        if pod_restart_count(worker_pod) != worker_restarts:
            fail("worker Pod restarted during the Redis outage")
        baseline = kind_e2e.event_status(baseline_id)
        if baseline.get("status") != "delivered" or not common.stream_contains(baseline_id):
            fail("baseline event state was not preserved on the PVC")

        recovery_id = kind_e2e.post_event("incident.redis.recovery")
        recovery = kind_e2e.wait_event_state(recovery_id, "delivered")
        if recovery.get("attempt_count") != 1:
            fail("post-recovery event did not deliver in one attempt")
        common.wait_metric(QUEUE_METRIC, lambda value: value == 0, "queue depth = 0")
        common.wait_metric(PENDING_METRIC, lambda value: value == 0, "pending = 0")
        common.wait_metric(
            REDIS_RECENT_FAILURES,
            lambda value: value == 0,
            "recent Redis failure increase = 0",
            timeout_seconds=75,
        )
        observability_runtime.wait_for_alert(
            "HooklaneRedisOperationFailures",
            {"inactive"},
            timeout_seconds=75,
        )
        print(
            "[ok] Redis outage drill: readiness failed while liveness stayed healthy, "
            f"false 202 was rejected, alert was {alert_state}, PVC/state were preserved, "
            "and delivery resumed"
        )


def main() -> int:
    owned_cluster = kind_runtime.CLUSTER_NAME not in kind_runtime.clusters()
    passed = True
    try:
        if owned_cluster:
            kind_runtime.cluster_up()
        run_drill()
    except (
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        passed = False
        print(f"[fail] Redis outage incident drill: {type(error).__name__}")
        if isinstance(error, RuntimeError):
            print(f"[fail-detail] {error}")
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            kind_e2e.write_diagnostics()
    finally:
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            try:
                scale_redis(1)
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] Redis incident StatefulSet restoration failed")
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters() and kind_e2e.release_exists():
            try:
                kind_e2e.restore_normal_release()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] Redis incident normal configuration restoration failed")
        if owned_cluster:
            try:
                kind_runtime.cluster_down()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] Redis incident cluster cleanup failed")
    if passed:
        print("[ok] incident-redis-outage completed without false success or data loss")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
