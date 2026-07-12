"""Run all incident drills and validate their operational receipts."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Never

import incident_downstream_5xx
import incident_redis_outage
import incident_worker_stop
import kind_e2e
import kind_runtime
import observability_runtime


ROOT = Path(__file__).resolve().parents[1]
SLO = ROOT / "docs" / "SLO.md"
DASHBOARD = (
    ROOT
    / "charts"
    / "hooklane"
    / "files"
    / "grafana"
    / "dashboards"
    / "hooklane-overview.json"
)
ALERT_RULES = (
    ROOT
    / "charts"
    / "hooklane"
    / "files"
    / "prometheus"
    / "rules"
    / "hooklane-alerts.yml"
)
POSTMORTEM = ROOT / "docs" / "incidents" / "postmortem-worker-stop.md"
INCIDENT_SECTIONS = (
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
    "## 検証receipt",
)
POSTMORTEM_SECTIONS = (
    "## Summary",
    "## Impact",
    "## Detection",
    "## Timeline",
    "## Root cause",
    "## Contributing factors",
    "## What went well",
    "## What went poorly",
    "## Recovery",
    "## Corrective actions",
    "## Action categories and status",
    "## Lessons learned",
    "## Limitations",
)
INCIDENT_CONTRACTS = {
    ROOT / "docs" / "incidents" / "downstream-5xx.md": (
        ROOT / "docs" / "runbooks" / "HooklaneDeliveryFailureRateHigh.md",
        (
            "HooklaneDeliveryFailureRateHigh",
            "HooklaneRetryRateHigh",
            "hooklane_delivery_outcomes_total",
            "hooklane_retry_scheduled_total",
            "Hooklane SLI and Operations",
        ),
    ),
    ROOT / "docs" / "incidents" / "redis-outage.md": (
        ROOT / "docs" / "runbooks" / "HooklaneRedisOperationFailures.md",
        (
            "HooklaneRedisOperationFailures",
            "hooklane_redis_operation_failures_total",
            "hooklane_enqueue_total",
            "Hooklane SLI and Operations",
        ),
    ),
    ROOT / "docs" / "incidents" / "worker-stop.md": (
        ROOT / "docs" / "runbooks" / "HooklaneQueueBacklogGrowing.md",
        (
            "HooklaneQueueBacklogGrowing",
            "HooklaneOldestEventTooOld",
            "hooklane_pending_messages",
            "hooklane_queue_depth",
            "hooklane_oldest_queued_event_age_seconds",
            "hooklane_worker_in_flight",
            "Hooklane SLI and Operations",
        ),
    ),
}
DRILLS: tuple[tuple[str, str, Callable[[], None]], ...] = (
    ("F024", "incident-downstream-5xx", incident_downstream_5xx.run_drill),
    ("F025", "incident-redis-outage", incident_redis_outage.run_drill),
    ("F026", "incident-worker-stop", incident_worker_stop.run_drill),
)
LINK_PATTERN = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def fail(message: str) -> Never:
    raise RuntimeError(message)


def validate_local_links(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    for raw_target in LINK_PATTERN.findall(text):
        target = raw_target.split("#", 1)[0]
        if not target or target.startswith(("http://", "https://")):
            continue
        resolved = (document.parent / target).resolve()
        if not resolved.is_relative_to(ROOT) or not resolved.is_file():
            fail(f"document link does not resolve: {document.relative_to(ROOT)}")


def validate_documents() -> None:
    slo_text = SLO.read_text(encoding="utf-8")
    rules_text = ALERT_RULES.read_text(encoding="utf-8")
    makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
    for incident, contract in INCIDENT_CONTRACTS.items():
        runbook, references = contract
        incident_text = incident.read_text(encoding="utf-8")
        runbook_text = runbook.read_text(encoding="utf-8")
        if any(section not in incident_text for section in INCIDENT_SECTIONS):
            fail(f"incident record is missing a required section: {incident.name}")
        if any(reference not in incident_text for reference in references):
            fail(f"incident record is missing an operational reference: {incident.name}")
        if f"../runbooks/{runbook.name}" not in incident_text:
            fail(f"incident record does not link its Runbook: {incident.name}")
        if f"../incidents/{incident.name}" not in runbook_text:
            fail(f"Runbook does not link its incident record: {runbook.name}")
        if f"incidents/{incident.name}" not in slo_text:
            fail(f"SLO does not link its incident receipt: {incident.name}")
        validate_local_links(incident)
        validate_local_links(runbook)

    postmortem_text = POSTMORTEM.read_text(encoding="utf-8")
    if any(section not in postmortem_text for section in POSTMORTEM_SECTIONS):
        fail("blameless postmortem is missing a required section")
    if "owner" in postmortem_text.lower():
        fail("public postmortem must use action categories and status, not owners")
    for reference in (
        "worker-stop.md",
        "HooklaneQueueBacklogGrowing",
        "HooklaneOldestEventTooOld",
        "../SLO.md#配送適時性",
        "hooklane-alerts.yml",
        "hooklane-overview.json",
        "hooklane_pending_messages",
        "hooklane_worker_in_flight",
    ):
        if reference not in postmortem_text:
            fail("blameless postmortem is missing an operational reference")
    queue_runbook = INCIDENT_CONTRACTS[
        ROOT / "docs" / "incidents" / "worker-stop.md"
    ][0].read_text(encoding="utf-8")
    if "../incidents/postmortem-worker-stop.md" not in queue_runbook:
        fail("worker Runbook does not link the blameless postmortem")
    validate_local_links(POSTMORTEM)
    validate_local_links(SLO)

    dashboard: object = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    if not isinstance(dashboard, dict) or dashboard.get("title") != "Hooklane SLI and Operations":
        fail("incident documents reference an unknown dashboard")
    for alert_name in (
        "HooklaneDeliveryFailureRateHigh",
        "HooklaneRetryRateHigh",
        "HooklaneRedisOperationFailures",
        "HooklaneQueueBacklogGrowing",
        "HooklaneOldestEventTooOld",
    ):
        if alert_name not in rules_text:
            fail("incident documents reference an unknown alert")
    for _feature_id, target, _drill in DRILLS:
        if f"{target}:" not in makefile_text:
            fail(f"aggregate references an unknown Make target: {target}")
    print(
        "[ok] 3 incident records, reciprocal Runbook/SLO/dashboard/alert links, "
        "and blameless postmortem passed"
    )


def run_drills() -> None:
    for feature_id, target, drill in DRILLS:
        started_at = time.monotonic()
        print(f"[drill:start] {feature_id} make {target}", flush=True)
        try:
            drill()
        except Exception as error:
            duration = time.monotonic() - started_at
            print(f"[drill:end] {feature_id} duration_seconds={duration:.2f}", flush=True)
            print(
                f"[drill:result] {feature_id} fail type={type(error).__name__}",
                flush=True,
            )
            raise
        duration = time.monotonic() - started_at
        print(f"[drill:end] {feature_id} duration_seconds={duration:.2f}", flush=True)
        print(f"[drill:result] {feature_id} pass", flush=True)


def workload_replicas(resource: str) -> int:
    workload = kind_e2e.kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        resource,
    )
    spec = workload.get("spec")
    replicas = spec.get("replicas") if isinstance(spec, dict) else None
    if not isinstance(replicas, int):
        fail(f"workload replica count is unavailable: {resource}")
    return replicas


def mock_sink_environment() -> dict[str, str]:
    deployment = kind_e2e.kubectl_json(
        "--namespace",
        kind_runtime.NAMESPACE,
        "get",
        "deployment/hooklane-mock-sink",
    )
    spec = deployment.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    if not isinstance(containers, list) or len(containers) != 1:
        fail("mock sink container configuration is unavailable")
    container = containers[0]
    environment = container.get("env") if isinstance(container, dict) else None
    if not isinstance(environment, list):
        fail("mock sink environment configuration is unavailable")
    values: dict[str, str] = {}
    for entry in environment:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if isinstance(name, str) and isinstance(value, str):
            values[name] = value
    return values


def verify_final_runtime() -> None:
    kind_e2e.wait_all_workloads()
    kind_e2e.wait_api_ready()
    if workload_replicas("deployment/hooklane-worker") != 1:
        fail("worker replica count did not recover to one")
    if workload_replicas("statefulset/hooklane-redis") != 1:
        fail("Redis replica count did not recover to one")
    sink_environment = mock_sink_environment()
    if sink_environment.get("HOOKLANE_MOCK_SINK_MODE") != "accept":
        fail("mock sink failure mode remained enabled")
    if sink_environment.get("HOOKLANE_MOCK_SINK_DELAY_SECONDS") != "0":
        fail("mock sink delay remained enabled")

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
        for expression, label in (
            ('max(hooklane_queue_depth{service=~"api|worker"})', "queue depth = 0"),
            ('max(hooklane_pending_messages{service=~"api|worker"})', "pending = 0"),
            (
                'max(hooklane_oldest_queued_event_age_seconds{service=~"api|worker"})',
                "oldest age = 0",
            ),
            ('sum(hooklane_worker_in_flight{service="worker"})', "in-flight = 0"),
        ):
            incident_downstream_5xx.wait_metric(
                expression,
                lambda value: value == 0,
                label,
            )
        for alert_name in (
            "HooklaneDeliveryFailureRateHigh",
            "HooklaneRetryRateHigh",
            "HooklaneRedisOperationFailures",
            "HooklaneQueueBacklogGrowing",
            "HooklaneOldestEventTooOld",
        ):
            observability_runtime.wait_for_alert(
                alert_name,
                {"inactive"},
                timeout_seconds=75,
            )
    print(
        "[ok] all drills recovered: accepted event loss=0, queue/pending/in-flight=0, "
        "alerts inactive, and no failure injection remains"
    )


def main() -> int:
    owned_cluster = kind_runtime.CLUSTER_NAME not in kind_runtime.clusters()
    passed = True
    normal_restored = False
    try:
        validate_documents()
        if owned_cluster:
            kind_runtime.cluster_up()
        run_drills()
        kind_e2e.restore_normal_release()
        normal_restored = True
        verify_final_runtime()
    except Exception as error:
        passed = False
        print(f"[fail] incident aggregate: {type(error).__name__}")
        if isinstance(error, RuntimeError):
            print(f"[fail-detail] {error}")
        if kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            try:
                kind_e2e.write_diagnostics()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                print("[fail] sanitized incident diagnostics collection failed")
    finally:
        if (
            not normal_restored
            and kind_runtime.CLUSTER_NAME in kind_runtime.clusters()
            and kind_e2e.release_exists()
        ):
            try:
                kind_e2e.restore_normal_release()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] incident aggregate normal configuration restoration failed")
        if owned_cluster and kind_runtime.CLUSTER_NAME in kind_runtime.clusters():
            try:
                kind_runtime.cluster_down()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                passed = False
                print("[fail] incident aggregate cluster cleanup failed")
    if passed:
        print("[ok] incident-smoke completed: F024/F025/F026 all passed and recovered")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
