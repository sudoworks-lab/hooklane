"""Exercise worker loss after a downstream side effect and pending recovery."""

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
INCIDENT_RECORD = ROOT / "docs" / "incidents" / "worker-stop.md"
RUNBOOK = ROOT / "docs" / "runbooks" / "HooklaneQueueBacklogGrowing.md"
AVAILABILITY_RUNBOOK = ROOT / "docs" / "runbooks" / "HooklaneWorkerUnavailable.md"
DELIVERY_CONTRACT = ROOT / "src" / "hooklane" / "delivery" / "sink.py"
IN_FLIGHT_METRIC = 'sum(hooklane_worker_in_flight{service="worker"})'
PENDING_METRIC = 'max(hooklane_pending_messages{service=~"api|worker"})'
QUEUE_METRIC = 'max(hooklane_queue_depth{service=~"api|worker"})'
OLDEST_METRIC = 'max(hooklane_oldest_queued_event_age_seconds{service=~"api|worker"})'
SUCCESS_METRIC = (
    'sum(hooklane_delivery_outcomes_total{service="worker",outcome="success"})'
)
WORKER_TARGET_ABSENT = (
    'absent(up{job="hooklane-applications",component="worker"})'
)
WORKER_TARGET_UP = 'sum(up{job="hooklane-applications",component="worker"})'
WORKER_READY = 'sum(hooklane_service_ready{service="worker"})'


def fail(message: str) -> Never:
    raise RuntimeError(message)


def wait_sink_receipts(event_id: str, minimum: int, *, timeout_seconds: float = 45) -> int:
    deadline = time.monotonic() + timeout_seconds
    count = 0
    while time.monotonic() < deadline:
        records = common.structured_records(common.component_logs("mock-sink"))
        count = sum(
            record.get("event") == "delivery_received"
            and record.get("event_id") == event_id
            and record.get("outcome") == "success"
            for record in records
        )
        if count >= minimum:
            return count
        time.sleep(1)
    fail(f"mock sink did not record {minimum} delivery attempts; count={count}")


def verify_worker_records(
    original_records: list[dict[str, Any]],
    replacement_records: list[dict[str, Any]],
    event_id: str,
) -> None:
    original_started = common.records_for_event(
        original_records,
        event_id,
        "delivery_started",
    )
    replacement_started = common.records_for_event(
        replacement_records,
        event_id,
        "delivery_started",
    )
    replacement_completed = common.records_for_event(
        replacement_records,
        event_id,
        "delivery_completed",
    )
    if not any(record.get("attempt") == 1 for record in original_started):
        fail("original worker did not log attempt 1")
    if not any(record.get("attempt") == 2 for record in replacement_started):
        fail("replacement worker did not log attempt 2")
    if not any(
        record.get("attempt") == 2
        and record.get("status") == "delivered"
        and record.get("outcome") == "success"
        for record in replacement_completed
    ):
        fail("replacement worker completion log is inconsistent")


def verify_log_safety(*log_texts: str) -> None:
    combined = "\n".join(log_texts).lower()
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
        fail("worker incident logs contain a forbidden marker")


def verify_incident_links() -> None:
    record = INCIDENT_RECORD.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    availability_runbook = AVAILABILITY_RUNBOOK.read_text(encoding="utf-8")
    contract = DELIVERY_CONTRACT.read_text(encoding="utf-8")
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
        fail("worker incident record is missing a required section")
    if "../incidents/worker-stop.md" not in runbook:
        fail("queue backlog Runbook does not link back to the incident drill")
    if "../incidents/worker-stop.md" not in availability_runbook:
        fail("worker availability Runbook does not link back to the incident drill")
    for reference in (
        "HooklaneWorkerUnavailable",
        "HooklaneQueueBacklogGrowing",
        "HooklaneOldestEventTooOld",
        "../runbooks/HooklaneWorkerUnavailable.md",
        "../runbooks/HooklaneQueueBacklogGrowing.md",
        "../SLO.md#配送適時性",
        "absent(up",
        "hooklane_pending_messages",
        "Hooklane SLI and Operations",
    ):
        if reference not in record:
            fail("worker incident record is missing an operational reference")
    if 'DELIVERY_GUARANTEE = "at-least-once"' not in contract:
        fail("delivery contract no longer declares at-least-once")
    if 'DOWNSTREAM_DEDUPLICATION_KEY = "event_id"' not in contract:
        fail("delivery contract no longer declares event ID deduplication")


def live_worker_metrics(pod_name: str) -> tuple[float, float]:
    metric_names = {
        "hooklane_worker_in_flight",
        "hooklane_delivery_attempts_total",
    }
    python_code = (
        "from urllib.request import urlopen; "
        "text=urlopen('http://127.0.0.1:9090/metrics', timeout=2).read().decode(); "
        "names={'hooklane_worker_in_flight','hooklane_delivery_attempts_total'}; "
        "print('\\n'.join(line for line in text.splitlines() "
        "if line and not line.startswith('#') and line.split('{',1)[0] in names))"
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
            python_code,
        ),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    values: dict[str, float] = {}
    for line in completed.stdout.splitlines():
        name = line.split("{", 1)[0]
        if name in metric_names:
            values[name] = float(line.rsplit(" ", 1)[-1])
    if set(values) != metric_names:
        fail("worker live metrics did not expose the required bounded series")
    return (
        values["hooklane_worker_in_flight"],
        values["hooklane_delivery_attempts_total"],
    )


def wait_no_worker_pods() -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pods = kind_e2e.kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "pods",
            "--selector",
            "app.kubernetes.io/component=worker",
        )
        active = []
        items = pods.get("items")
        if isinstance(items, list):
            active = [
                item
                for item in items
                if isinstance(item, dict)
                and isinstance(item.get("metadata"), dict)
                and cast(dict[str, Any], item["metadata"]).get("deletionTimestamp") is None
            ]
        if not active:
            return
        time.sleep(1)
    fail("worker Pod did not stop")


def stop_worker_container(pod_name: str) -> None:
    pod = kind_e2e.kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "pod",
        pod_name,
    )
    status = pod.get("status")
    if not isinstance(status, dict):
        fail("worker Pod status is unavailable")
    container_statuses = status.get("containerStatuses")
    if not isinstance(container_statuses, list):
        fail("worker container status is unavailable")
    container_id: str | None = None
    for item in container_statuses:
        if not isinstance(item, dict) or item.get("name") != "worker":
            continue
        candidate = item.get("containerID")
        if isinstance(candidate, str):
            container_id = candidate
        break
    if container_id is None or not container_id.startswith("containerd://"):
        fail("worker container runtime is not the expected local containerd")
    nodes = [
        line.strip()
        for line in kind_runtime.output(
            ["kind", "get", "nodes", "--name", kind_runtime.CLUSTER_NAME]
        ).splitlines()
        if line.strip()
    ]
    if len(nodes) != 1:
        fail("local kind cluster does not have exactly one control-plane node")
    subprocess.run(
        [
            "docker",
            "exec",
            nodes[0],
            "crictl",
            "stop",
            "--timeout=0",
            container_id.removeprefix("containerd://"),
        ],
        cwd=ROOT,
        check=True,
        text=True,
        timeout=10,
    )


def wait_single_mock_sink_pod() -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        pods = kind_e2e.kubectl_json(
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "pods",
            "--selector",
            "app.kubernetes.io/component=mock-sink",
        )
        items = pods.get("items")
        if isinstance(items, list) and len(items) == 1:
            item = items[0]
            if isinstance(item, dict) and isinstance(item.get("metadata"), dict):
                metadata = cast(dict[str, Any], item["metadata"])
                if metadata.get("deletionTimestamp") is None:
                    return
        time.sleep(1)
    fail("previous mock sink Pod did not terminate")


def restore_sink_while_worker_stopped() -> None:
    kind_e2e.configure_sink_helm("accept", 0, worker_replica_count=0)
    wait_single_mock_sink_pod()
    worker = kind_e2e.kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "deployment/hooklane-worker",
    )
    spec = worker.get("spec")
    if not isinstance(spec, dict) or spec.get("replicas") != 0:
        fail("sink recovery restarted the worker before the failure injection ended")


def run_drill() -> None:
    kind_runtime.deploy()
    kind_e2e.deploy_e2e_release()
    kind_runtime.helm_test()
    kind_e2e.wait_api_ready()
    verify_incident_links()
    baseline_id = kind_e2e.post_event("incident.worker.baseline")
    baseline = kind_e2e.wait_event_state(baseline_id, "delivered")
    if baseline.get("attempt_count") != 1:
        fail("baseline event did not deliver in one attempt")
    kind_e2e.configure_sink_helm("post_receipt_delay", 20)
    wait_single_mock_sink_pod()
    kind_e2e.run_kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "rollout",
        "restart",
        "deployment/hooklane-worker",
    )
    kind_e2e.wait_rollout("deployment/hooklane-worker")
    worker_scaled_down = False

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
        original_worker = kind_e2e.worker_pod_name()
        event_id = kind_e2e.post_event("incident.worker.stop")
        kind_e2e.wait_event_state(event_id, "delivering", timeout_seconds=45)
        in_flight, attempts = live_worker_metrics(original_worker)
        if in_flight != 1 or attempts != 1:
            fail(
                "worker live metrics did not capture one in-flight first attempt; "
                f"in_flight={in_flight}, attempts={attempts}"
            )
        initial_receipts = wait_sink_receipts(event_id, 1)
        if initial_receipts != 1:
            fail("original worker produced more than one downstream side effect")
        original_sink_log_text = common.component_logs("mock-sink")
        original_log_text = common.component_logs("worker")
        original_records = common.structured_records(original_log_text)

        try:
            kind_e2e.run_kubectl(
                "--namespace",
                kind_runtime.NAMESPACE,
                "scale",
                "deployment/hooklane-worker",
                "--replicas=0",
            )
            worker_scaled_down = True
            stop_worker_container(original_worker)
            kind_e2e.run_kubectl(
                "--namespace",
                kind_runtime.NAMESPACE,
                "delete",
                "pod",
                original_worker,
                "--grace-period=0",
                "--force",
                "--wait=false",
            )
            wait_no_worker_pods()
            common.wait_metric(
                WORKER_TARGET_ABSENT,
                lambda value: value == 1,
                "worker scrape target absent",
            )
            common.wait_metric(PENDING_METRIC, lambda value: value >= 1, "pending >= 1")
            common.wait_metric(QUEUE_METRIC, lambda value: value >= 1, "queue depth >= 1")
            pending_record = kind_e2e.event_status(event_id)
            if pending_record.get("status") != "delivering":
                fail("stopped worker event did not remain delivering")
            if pending_record.get("attempt_count") != 1:
                fail("stopped worker event attempt count changed before claim")
            availability_alert = observability_runtime.wait_for_alert(
                "HooklaneWorkerUnavailable",
                {"pending", "firing"},
                timeout_seconds=60,
            )
            backlog_alert = observability_runtime.wait_for_alert(
                "HooklaneQueueBacklogGrowing",
                {"pending", "firing"},
                timeout_seconds=60,
            )
            common.wait_metric(OLDEST_METRIC, lambda value: value > 20, "oldest age > 20")
            oldest_alert = observability_runtime.wait_for_alert(
                "HooklaneOldestEventTooOld",
                {"pending", "firing"},
                timeout_seconds=60,
            )
        finally:
            if worker_scaled_down:
                try:
                    restore_sink_while_worker_stopped()
                finally:
                    kind_e2e.run_kubectl(
                        "--namespace",
                        kind_runtime.NAMESPACE,
                        "scale",
                        "deployment/hooklane-worker",
                        "--replicas=1",
                    )
                    kind_e2e.wait_rollout("deployment/hooklane-worker")
                    worker_scaled_down = False

        replacement_worker = kind_e2e.worker_pod_name()
        if replacement_worker == original_worker:
            fail("worker Pod was not replaced")
        common.wait_metric(
            WORKER_TARGET_UP,
            lambda value: value >= 1,
            "worker scrape target up",
        )
        common.wait_metric(WORKER_READY, lambda value: value >= 1, "worker ready = 1")
        delivered = kind_e2e.wait_event_state(event_id, "delivered", timeout_seconds=120)
        if delivered.get("attempt_count") != 2:
            fail("replacement worker did not preserve the attempt transition to 2")
        replacement_receipts = wait_sink_receipts(event_id, 1, timeout_seconds=60)
        receipt_attempts = initial_receipts + replacement_receipts
        if receipt_attempts != 2:
            fail("downstream did not observe exactly two attempts for one event ID")
        replacement_log_text = common.component_logs("worker")
        replacement_sink_log_text = common.component_logs("mock-sink")
        replacement_records = common.structured_records(replacement_log_text)
        verify_worker_records(original_records, replacement_records, event_id)
        verify_log_safety(
            original_log_text,
            replacement_log_text,
            original_sink_log_text,
            replacement_sink_log_text,
        )
        if not common.stream_contains(event_id):
            fail("accepted worker incident event is missing from the Redis stream")
        common.wait_metric(
            SUCCESS_METRIC,
            lambda value: value >= 1,
            "replacement delivery success observed",
        )
        common.wait_metric(PENDING_METRIC, lambda value: value == 0, "pending = 0")
        common.wait_metric(QUEUE_METRIC, lambda value: value == 0, "queue depth = 0")
        common.wait_metric(OLDEST_METRIC, lambda value: value == 0, "oldest age = 0")
        common.wait_metric(IN_FLIGHT_METRIC, lambda value: value == 0, "in-flight = 0")
        observability_runtime.wait_for_alert(
            "HooklaneQueueBacklogGrowing",
            {"inactive"},
            timeout_seconds=75,
        )
        observability_runtime.wait_for_alert(
            "HooklaneOldestEventTooOld",
            {"inactive"},
            timeout_seconds=75,
        )
        observability_runtime.wait_for_alert(
            "HooklaneWorkerUnavailable",
            {"inactive"},
            timeout_seconds=75,
        )
        recovery_id = kind_e2e.post_event("incident.worker.recovery")
        recovery = kind_e2e.wait_event_state(recovery_id, "delivered")
        if recovery.get("attempt_count") != 1:
            fail("new event did not deliver normally after worker recovery")
        if observability_runtime.mock_sink_mode() != "accept":
            fail("worker incident failure injection remained enabled")
        print(
            "[ok] worker stop drill: side effect preceded ack, pending was reclaimed at "
            f"attempt 2, sink observed {receipt_attempts} deliveries for one event ID, "
            "the worker target became absent, "
            f"alerts were {availability_alert}/{backlog_alert}/{oldest_alert}, "
            "availability recovered to inactive, and no event was lost"
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
        print(f"[fail] worker stop incident drill: {type(error).__name__}")
        if isinstance(error, RuntimeError):
            print(f"[fail-detail] {error}")
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            kind_e2e.write_diagnostics()
    finally:
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            try:
                kind_e2e.run_kubectl(
                    "--namespace",
                    kind_runtime.NAMESPACE,
                    "scale",
                    "deployment/hooklane-worker",
                    "--replicas=1",
                )
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] worker incident replica restoration failed")
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters() and kind_e2e.release_exists():
            try:
                kind_e2e.restore_normal_release()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] worker incident normal configuration restoration failed")
        if owned_cluster:
            try:
                kind_runtime.cluster_down()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] worker incident cluster cleanup failed")
    if passed:
        print("[ok] incident-worker-stop completed with pending recovery and no event loss")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
