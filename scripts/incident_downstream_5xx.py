"""Exercise downstream 5xx detection, retry, recovery, and data integrity."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Literal, Never, cast

import kind_e2e
import kind_runtime
import observability_runtime
from hooklane.observability.normalized_signal import (
    DeliveryFailureRateObservation,
    normalize_delivery_failure_rate,
)


ROOT = Path(__file__).resolve().parents[1]
INCIDENT_RECORD = ROOT / "docs" / "incidents" / "downstream-5xx.md"
RUNBOOK = ROOT / "docs" / "runbooks" / "HooklaneDeliveryFailureRateHigh.md"
FAILURE_METRIC = (
    'sum(hooklane_delivery_outcomes_total{service="worker",'
    'outcome=~"retry_scheduled|dead_lettered|pending|failure"})'
)
FAILURE_RATE_METRIC = (
    'sum(rate(hooklane_delivery_outcomes_total{service="worker",'
    'outcome=~"retry_scheduled|dead_lettered|pending|failure"}[30s]))'
    ' / clamp_min(sum(rate(hooklane_delivery_outcomes_total{service="worker"}[30s])), '
    '0.000000001)'
)
RETRY_METRIC = 'sum(hooklane_retry_scheduled_total{service="worker"})'
QUEUE_METRIC = 'max(hooklane_queue_depth{service=~"api|worker"})'
OLDEST_METRIC = 'max(hooklane_oldest_queued_event_age_seconds{service=~"api|worker"})'
PENDING_METRIC = 'sum(hooklane_pending_messages{service="worker"})'
FAILURE_RATE_THRESHOLD = 0.20
FAILURE_RATE_WINDOW_SECONDS = 30.0
FAILURE_RATE_HOLD_SECONDS = 10.0
NORMALIZED_SOURCE_REF = "docs/incidents/downstream-5xx.md"
NORMALIZED_EVIDENCE_REFS = [
    "docs/incidents/downstream-5xx.md",
    "docs/runbooks/HooklaneDeliveryFailureRateHigh.md",
    "docs/runbooks/HooklaneRetryRateHigh.md",
]
LOG_FIELDS = frozenset(
    {
        "timestamp",
        "level",
        "service",
        "event",
        "request_id",
        "event_id",
        "attempt",
        "status",
        "outcome",
        "reason_code",
        "duration_ms",
    }
)
FORBIDDEN_LOG_FIELDS = frozenset(
    {
        "payload",
        "idempotency_key",
        "credential",
        "redis_password",
        "redis_url",
        "cookie",
        "exception",
        "stack_trace",
    }
)


def fail(message: str) -> Never:
    raise RuntimeError(message)


def wait_metric(
    expression: str,
    predicate: Callable[[float], bool],
    label: str,
    *,
    timeout_seconds: float = 75,
) -> float:
    deadline = time.monotonic() + timeout_seconds
    value = 0.0
    while time.monotonic() < deadline:
        value = observability_runtime.query_value(expression)
        if predicate(value):
            return value
        time.sleep(2)
    fail(f"metric did not satisfy {label}; value={value}")


def component_logs(component: str) -> str:
    completed = subprocess.run(
        kind_e2e.kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "logs",
            f"deployment/hooklane-{component}",
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


def structured_records(log_text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        if not line.startswith("{"):
            continue
        parsed: object = json.loads(line)
        if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
            fail("structured log line is not an object with string keys")
        record = cast(dict[str, Any], parsed)
        if not set(record).issubset(LOG_FIELDS):
            fail("structured log contains a field outside the public contract")
        if set(record).intersection(FORBIDDEN_LOG_FIELDS):
            fail("structured log contains a forbidden field")
        records.append(record)
    if not records:
        fail("component emitted no structured JSON records")
    return records


def records_for_event(
    records: list[dict[str, Any]],
    event_id: str,
    event_name: str,
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("event_id") == event_id and record.get("event") == event_name
    ]


def verify_failure_logs(event_ids: list[str]) -> None:
    worker_text = component_logs("worker")
    sink_text = component_logs("mock-sink")
    forbidden_text = (
        "payload",
        "credential",
        "password",
        "private key",
        "redis://",
        "idempotency-key",
        "cookie",
    )
    combined = f"{worker_text}\n{sink_text}".lower()
    if any(marker in combined for marker in forbidden_text):
        fail("runtime logs contain a forbidden diagnostic marker")
    worker_records = structured_records(worker_text)
    sink_records = structured_records(sink_text)
    for event_id in event_ids:
        retries = records_for_event(worker_records, event_id, "retry_scheduled")
        if not retries or not any(
            record.get("reason_code") == "http_5xx"
            and isinstance(record.get("attempt"), int)
            and cast(int, record["attempt"]) >= 1
            for record in retries
        ):
            fail("worker retry log does not correlate event, attempt, and http_5xx")
        receipts = records_for_event(sink_records, event_id, "delivery_received")
        if not receipts or not any(
            record.get("outcome") == "failure"
            and record.get("reason_code") == "http_5xx"
            for record in receipts
        ):
            fail("mock sink failure receipt is missing for an injected event")
    print("[ok] structured worker and sink logs correlate event IDs, attempts, and http_5xx")


def verify_success_receipts(event_ids: list[str]) -> None:
    sink_records = structured_records(component_logs("mock-sink"))
    for event_id in event_ids:
        receipts = records_for_event(sink_records, event_id, "delivery_received")
        if not any(
            record.get("outcome") == "success"
            and record.get("reason_code") == "none"
            for record in receipts
        ):
            fail("mock sink success receipt is missing after recovery")


def stream_contains(event_id: str) -> bool:
    lua = (
        "local rows=redis.call('XRANGE',KEYS[1],'-','+');"
        "for _,row in ipairs(rows) do local fields=row[2];"
        "for i=1,#fields,2 do if fields[i]=='event_id' and fields[i+1]==ARGV[1] "
        "then return 1 end end end;return 0"
    )
    completed = subprocess.run(
        kind_e2e.kubectl_command(
            "--namespace",
            kind_runtime.NAMESPACE,
            "exec",
            "statefulset/hooklane-redis",
            "--",
            "redis-cli",
            "--raw",
            "EVAL",
            lua,
            "1",
            "hooklane:events",
            event_id,
        ),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip() == "1"


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
        fail("downstream incident record is missing a required section")
    required_references = (
        "HooklaneDeliveryFailureRateHigh",
        "HooklaneRetryRateHigh",
        "HooklaneQueueBacklogGrowing",
        "../runbooks/HooklaneDeliveryFailureRateHigh.md",
        "../SLO.md#配送成功率",
        "hooklane_delivery_outcomes_total",
        "hooklane_retry_scheduled_total",
        "Hooklane SLI and Operations",
    )
    if any(reference not in record for reference in required_references):
        fail("downstream incident record is missing an operational reference")
    if "../incidents/downstream-5xx.md" not in runbook:
        fail("delivery failure Runbook does not link back to the incident drill")


def ops_captured_at(timestamp: str) -> str:
    """Adapt Prometheus alert timestamps to the ops manifest's millisecond contract."""

    if "." not in timestamp:
        return timestamp
    base, fraction = timestamp[:-1].split(".", 1)
    return f"{base}.{fraction[:3].ljust(3, '0')}Z"


def write_normalized_delivery_failure_signal(
    output_path: Path | None,
    *,
    injected_event_id: str,
    failure_alert_observation: dict[str, str],
    failure_rate: float,
    signal_id: str | None = None,
    correlation_id: str | None = None,
    observed_at: str | None = None,
) -> None:
    if output_path is None:
        return
    resolved_path = output_path.resolve()
    if resolved_path.is_relative_to(ROOT):
        fail("normalized output must be outside the Hooklane repository")
    active_at = observed_at or failure_alert_observation.get("active_at", "")
    alert_state = failure_alert_observation.get("state", "")
    if not active_at or alert_state not in {"pending", "firing"}:
        fail("normalized output requires an observed delivery failure alert state and timestamp")
    observation = DeliveryFailureRateObservation(
        signal_id=signal_id or f"downstream-5xx-{injected_event_id}",
        observed_at=active_at,
        correlation_id=correlation_id or injected_event_id,
        alert_state=cast(Literal["pending", "firing"], alert_state),
        failure_rate=failure_rate,
        threshold=FAILURE_RATE_THRESHOLD,
        window_seconds=FAILURE_RATE_WINDOW_SECONDS,
        required_duration_seconds=FAILURE_RATE_HOLD_SECONDS,
        source_ref=NORMALIZED_SOURCE_REF,
        captured_at=ops_captured_at(active_at),
        evidence_refs=NORMALIZED_EVIDENCE_REFS,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(normalize_delivery_failure_rate(observation))
    print(f"[ok] normalized delivery failure-rate signal written to {output_path}")


def run_drill(
    *,
    normalized_output: Path | None = None,
    normalized_signal_id: str | None = None,
    normalized_correlation_id: str | None = None,
    normalized_observed_at: str | None = None,
) -> None:
    kind_runtime.deploy()
    kind_e2e.deploy_e2e_release()
    kind_runtime.helm_test()
    kind_e2e.wait_api_ready()
    verify_incident_links()
    injected_ids: list[str] = []
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
        baseline_id = kind_e2e.post_event("incident.downstream.baseline")
        baseline = kind_e2e.wait_event_state(baseline_id, "delivered")
        if baseline.get("attempt_count") != 1:
            fail("baseline delivery did not complete in one attempt")

        failure_before = observability_runtime.query_value(FAILURE_METRIC)
        retry_before = observability_runtime.query_value(RETRY_METRIC)
        try:
            kind_e2e.configure_sink_helm("server_error", 0)
            injected_ids = [
                kind_e2e.post_event("incident.downstream.failure") for _index in range(3)
            ]
            wait_metric(
                FAILURE_METRIC,
                lambda value: value >= failure_before + 3,
                "delivery failures +3",
            )
            wait_metric(
                RETRY_METRIC,
                lambda value: value >= retry_before + 3,
                "retry scheduled +3",
            )
            wait_metric(QUEUE_METRIC, lambda value: value >= 1, "queue depth >= 1")
            wait_metric(OLDEST_METRIC, lambda value: value > 0, "oldest age > 0")
            delivery_alert_observation = observability_runtime.wait_for_alert_observation(
                "HooklaneDeliveryFailureRateHigh",
                {"pending", "firing"},
                timeout_seconds=60,
            )
            delivery_alert = delivery_alert_observation["state"]
            failure_rate = wait_metric(
                FAILURE_RATE_METRIC,
                lambda value: value > FAILURE_RATE_THRESHOLD,
                "delivery failure rate > 20 percent",
            )
            retry_alert = observability_runtime.wait_for_alert(
                "HooklaneRetryRateHigh",
                {"pending", "firing"},
                timeout_seconds=60,
            )
            verify_failure_logs(injected_ids)
            write_normalized_delivery_failure_signal(
                normalized_output,
                injected_event_id=injected_ids[0],
                failure_alert_observation=delivery_alert_observation,
                failure_rate=failure_rate,
                signal_id=normalized_signal_id,
                correlation_id=normalized_correlation_id,
                observed_at=normalized_observed_at,
            )
        finally:
            kind_e2e.configure_sink_helm("accept", 0)

        delivered_records = [
            kind_e2e.wait_event_state(event_id, "delivered", timeout_seconds=120)
            for event_id in injected_ids
        ]
        if any(
            not isinstance(record.get("attempt_count"), int)
            or cast(int, record["attempt_count"]) < 2
            for record in delivered_records
        ):
            fail("recovered delivery did not preserve retry attempt counts")
        if not all(stream_contains(event_id) for event_id in injected_ids):
            fail("an accepted incident event is missing from the Redis stream")
        verify_success_receipts(injected_ids)

        recovery_id = kind_e2e.post_event("incident.downstream.recovery")
        recovery = kind_e2e.wait_event_state(recovery_id, "delivered")
        if recovery.get("attempt_count") != 1:
            fail("new recovery event did not deliver in one attempt")
        wait_metric(QUEUE_METRIC, lambda value: value == 0, "queue depth = 0")
        wait_metric(PENDING_METRIC, lambda value: value == 0, "pending = 0")
        wait_metric(OLDEST_METRIC, lambda value: value == 0, "oldest age = 0")
        observability_runtime.wait_for_alert(
            "HooklaneDeliveryFailureRateHigh",
            {"inactive"},
            timeout_seconds=90,
        )
        observability_runtime.wait_for_alert(
            "HooklaneRetryRateHigh",
            {"inactive"},
            timeout_seconds=90,
        )
        if observability_runtime.mock_sink_mode() != "accept":
            fail("downstream failure injection remained enabled")
        print(
            "[ok] downstream 5xx drill: failures/retries/backlog increased, alerts were "
            f"{delivery_alert}/{retry_alert}, all three events were retained and delivered, "
            "and queue/pending/alerts recovered"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized-output", type=Path)
    parser.add_argument("--normalized-signal-id")
    parser.add_argument("--normalized-correlation-id")
    parser.add_argument("--normalized-observed-at")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    owned_cluster = kind_runtime.CLUSTER_NAME not in kind_runtime.clusters()
    passed = True
    try:
        if owned_cluster:
            kind_runtime.cluster_up()
        run_drill(
            normalized_output=args.normalized_output,
            normalized_signal_id=args.normalized_signal_id,
            normalized_correlation_id=args.normalized_correlation_id,
            normalized_observed_at=args.normalized_observed_at,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        passed = False
        print(f"[fail] downstream 5xx incident drill: {type(error).__name__}")
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            kind_e2e.write_diagnostics()
    finally:
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters() and kind_e2e.release_exists():
            try:
                kind_e2e.restore_normal_release()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] downstream incident normal configuration restoration failed")
        if owned_cluster:
            try:
                kind_runtime.cluster_down()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] downstream incident cluster cleanup failed")
    if passed:
        print("[ok] incident-downstream-5xx completed with no accepted event loss")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
