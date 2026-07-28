"""Deploy and verify Hooklane observability in the project-specific kind cluster."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Never, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import kind_runtime


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (
    ROOT
    / "charts"
    / "hooklane"
    / "files"
    / "grafana"
    / "dashboards"
    / "hooklane-overview.json"
)
SLI_PROMQL = ROOT / "observability" / "sli-promql.json"
PROMETHEUS_LOCAL_PORT = 19090
GRAFANA_LOCAL_PORT = 13000


def fail(message: str) -> Never:
    raise RuntimeError(message)


def request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 5,
) -> dict[str, Any]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urlopen(request, timeout=timeout) as response:
        parsed: object = json.loads(response.read().decode())
    if not isinstance(parsed, dict):
        fail("HTTP API returned a non-object JSON response")
    return cast(dict[str, Any], parsed)


def wait_json(url: str, *, timeout_seconds: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "endpoint unavailable"
    while time.monotonic() < deadline:
        try:
            return request_json(url)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
            time.sleep(1)
    fail(f"endpoint did not become ready: {last_error}")


@contextmanager
def port_forward(service: str, local_port: int, remote_port: int) -> Iterator[None]:
    command = [
        "kubectl",
        "--kubeconfig",
        str(kind_runtime.KUBECONFIG),
        "--context",
        kind_runtime.CONTEXT_NAME,
        "--namespace",
        kind_runtime.NAMESPACE,
        "port-forward",
        "--address=127.0.0.1",
        f"service/{service}",
        f"{local_port}:{remote_port}",
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def helm_observability_upgrade() -> None:
    kind_runtime.require_cluster()
    image_tag = kind_runtime.resolve_image_tag()
    kind_runtime.load_images(image_tag)
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
        "--wait",
        "--timeout",
        "240s",
        "--history-max",
        "3",
        *kind_runtime.image_overrides(image_tag),
    )
    for workload in (
        "deployment/hooklane-api",
        "deployment/hooklane-worker",
        "deployment/hooklane-mock-sink",
        "statefulset/hooklane-redis",
        "deployment/hooklane-prometheus",
        "deployment/hooklane-grafana",
    ):
        kind_runtime.kubectl(
            "--namespace",
            kind_runtime.NAMESPACE,
            "rollout",
            "status",
            workload,
            "--timeout=240s",
        )
    print("[ok] Hooklane, Prometheus, and Grafana workloads are Ready")


def up() -> None:
    if kind_runtime.CLUSTER_NAME not in kind_runtime.clusters():
        kind_runtime.cluster_up()
    helm_observability_upgrade()


def prometheus_query(expression: str) -> dict[str, Any]:
    url = f"http://127.0.0.1:{PROMETHEUS_LOCAL_PORT}/api/v1/query?{urlencode({'query': expression})}"
    response = request_json(url)
    if response.get("status") != "success":
        fail("Prometheus rejected a query")
    return response


def query_value(expression: str) -> float:
    response = prometheus_query(expression)
    data = response.get("data")
    if not isinstance(data, dict):
        return 0.0
    result = data.get("result")
    if not isinstance(result, list) or not result:
        return 0.0
    first = result[0]
    if not isinstance(first, dict):
        return 0.0
    value = first.get("value")
    if not isinstance(value, list) or len(value) != 2:
        return 0.0
    return float(value[1])


def wait_for_targets() -> dict[str, Any]:
    deadline = time.monotonic() + 90
    last_counts: dict[str, int] = {}
    while time.monotonic() < deadline:
        targets = request_json(
            f"http://127.0.0.1:{PROMETHEUS_LOCAL_PORT}/api/v1/targets"
        )
        data = targets.get("data")
        active = data.get("activeTargets", []) if isinstance(data, dict) else []
        counts = {"api": 0, "worker": 0, "mock-sink": 0}
        for target_object in active if isinstance(active, list) else []:
            if not isinstance(target_object, dict) or target_object.get("health") != "up":
                continue
            labels = target_object.get("labels")
            if not isinstance(labels, dict):
                continue
            component = labels.get("component")
            if component in counts:
                counts[cast(str, component)] += 1
        last_counts = counts
        if counts["api"] >= 2 and counts["worker"] >= 1 and counts["mock-sink"] >= 1:
            return targets
        time.sleep(2)
    fail(f"application scrape targets did not become UP: {last_counts}")


def wait_for_metric(expression: str, minimum: float, *, timeout_seconds: float = 60) -> float:
    deadline = time.monotonic() + timeout_seconds
    value = 0.0
    while time.monotonic() < deadline:
        value = query_value(expression)
        if value >= minimum:
            return value
        time.sleep(2)
    fail(f"metric did not reach the expected minimum: {expression}={value}")


def submit_event(marker: str) -> str:
    accepted = request_json(
        "http://127.0.0.1:18082/v1/events",
        payload={"event_type": "observability.test", "payload": {"marker": marker}},
    )
    event_id = accepted.get("event_id")
    if not isinstance(event_id, str):
        fail("event acceptance did not return an event ID")
    return event_id


def wait_for_delivery(event_id: str, *, timeout_seconds: float = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = request_json(f"http://127.0.0.1:18082/v1/events/{event_id}")
        if status.get("status") == "delivered":
            return
        if status.get("status") == "dead_letter":
            fail("event reached dead-letter while waiting for recovery")
        time.sleep(0.5)
    fail("event did not reach delivered status")


def wait_for_terminal_status(event_id: str, *, timeout_seconds: float = 75) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = request_json(f"http://127.0.0.1:18082/v1/events/{event_id}")
        value = status.get("status")
        if value in {"delivered", "dead_letter"}:
            return cast(str, value)
        time.sleep(0.5)
    fail("injected event did not reach a terminal status")


def submit_and_wait_for_delivery(marker: str) -> str:
    event_id = submit_event(marker)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = request_json(f"http://127.0.0.1:18082/v1/events/{event_id}")
        if status.get("status") == "delivered":
            return event_id
        time.sleep(0.5)
    fail("event did not reach delivered status")


def dashboard_queries() -> list[str]:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    expressions: list[str] = []
    for panel in dashboard["panels"]:
        expressions.extend(target["expr"] for target in panel["targets"])
    return expressions


def string_values(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from string_values(nested)


def set_mock_sink_mode(mode: str) -> None:
    if mode not in {"accept", "server_error"}:
        fail("unsupported mock sink mode")
    kind_runtime.helm(
        "upgrade",
        "--install",
        kind_runtime.RELEASE,
        str(kind_runtime.CHART),
        "--namespace",
        kind_runtime.NAMESPACE,
        "--set",
        "observability.enabled=true",
        "--set",
        f"mockSink.failureMode={mode}",
        "--wait",
        "--timeout",
        "180s",
        "--history-max",
        "3",
    )
    kind_runtime.kubectl(
        "--namespace",
        kind_runtime.NAMESPACE,
        "rollout",
        "status",
        "deployment/hooklane-mock-sink",
        "--timeout=180s",
    )


def alert_states() -> dict[str, str]:
    response = request_json(
        f"http://127.0.0.1:{PROMETHEUS_LOCAL_PORT}/api/v1/alerts"
    )
    data = response.get("data")
    alerts = data.get("alerts", []) if isinstance(data, dict) else []
    states: dict[str, str] = {}
    for alert_object in alerts if isinstance(alerts, list) else []:
        if not isinstance(alert_object, dict):
            continue
        labels = alert_object.get("labels")
        state = alert_object.get("state")
        name = labels.get("alertname") if isinstance(labels, dict) else None
        if isinstance(name, str) and isinstance(state, str):
            states[name] = state
    return states


def wait_for_alert(
    alert_name: str,
    expected_states: set[str],
    *,
    timeout_seconds: float = 60,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    state = "inactive"
    while time.monotonic() < deadline:
        state = alert_states().get(alert_name, "inactive")
        if state in expected_states:
            return state
        time.sleep(2)
    fail(f"alert {alert_name} did not reach {sorted(expected_states)}; state={state}")


def loaded_alert_names() -> set[str]:
    response = request_json(
        f"http://127.0.0.1:{PROMETHEUS_LOCAL_PORT}/api/v1/rules?type=alert"
    )
    data = response.get("data")
    groups = data.get("groups", []) if isinstance(data, dict) else []
    names: set[str] = set()
    for group_object in groups if isinstance(groups, list) else []:
        if not isinstance(group_object, dict):
            continue
        rules = group_object.get("rules")
        for rule_object in rules if isinstance(rules, list) else []:
            if isinstance(rule_object, dict) and isinstance(rule_object.get("name"), str):
                names.add(cast(str, rule_object["name"]))
    return names


def mock_sink_mode() -> str:
    return kind_runtime.output(
        [
            "kubectl",
            "--kubeconfig",
            str(kind_runtime.KUBECONFIG),
            "--context",
            kind_runtime.CONTEXT_NAME,
            "--namespace",
            kind_runtime.NAMESPACE,
            "get",
            "deployment/hooklane-mock-sink",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='HOOKLANE_MOCK_SINK_MODE')].value}",
        ]
    ).strip()


def smoke_base() -> None:
    kind_runtime.require_cluster()
    prometheus_service = f"{kind_runtime.RELEASE}-prometheus"
    grafana_service = f"{kind_runtime.RELEASE}-grafana"
    marker = f"observability-{uuid4()}"
    with port_forward(
        prometheus_service,
        PROMETHEUS_LOCAL_PORT,
        9090,
    ), port_forward(grafana_service, GRAFANA_LOCAL_PORT, 3000):
        wait_json(f"http://127.0.0.1:{PROMETHEUS_LOCAL_PORT}/api/v1/status/buildinfo")
        wait_json(f"http://127.0.0.1:{GRAFANA_LOCAL_PORT}/api/health")
        targets = wait_for_targets()
        enqueue_before = query_value(
            'sum(hooklane_enqueue_total{service="api",outcome="success"})'
        )
        delivery_before = query_value(
            'sum(hooklane_delivery_outcomes_total{service="worker",outcome="success"})'
        )
        event_id = submit_and_wait_for_delivery(marker)
        wait_for_metric(
            'sum(hooklane_enqueue_total{service="api",outcome="success"})',
            enqueue_before + 1,
        )
        wait_for_metric(
            'sum(hooklane_delivery_outcomes_total{service="worker",outcome="success"})',
            delivery_before + 1,
        )
        deadline = time.monotonic() + 60
        queue_depth = -1.0
        while time.monotonic() < deadline:
            queue_depth = query_value(
                'max(hooklane_queue_depth{service=~"api|worker"})'
            )
            if queue_depth == 0:
                break
            time.sleep(2)
        if queue_depth != 0:
            fail(f"queue depth did not return to zero: {queue_depth}")
        available_api = wait_for_metric(
            'sum(hooklane_service_ready{service="api"})',
            2,
        )

        sli: object = json.loads(SLI_PROMQL.read_text(encoding="utf-8"))
        if not isinstance(sli, dict):
            fail("SLI PromQL mapping must be an object")
        for expression in [*dashboard_queries(), *cast(dict[str, str], sli).values()]:
            prometheus_query(expression)

        dashboard = wait_json(
            f"http://127.0.0.1:{GRAFANA_LOCAL_PORT}/api/dashboards/uid/hooklane-overview"
        )
        provisioned = dashboard.get("dashboard")
        if not isinstance(provisioned, dict) or provisioned.get("title") != (
            "Hooklane SLI and Operations"
        ):
            fail("Grafana dashboard was not provisioned")
        datasource = wait_json(
            f"http://127.0.0.1:{GRAFANA_LOCAL_PORT}/api/datasources/uid/hooklane-prometheus"
        )
        if datasource.get("type") != "prometheus" or datasource.get("url") != (
            "http://hooklane-prometheus:9090"
        ):
            fail("Grafana Prometheus datasource does not match the cluster service")
        datasource_health = wait_json(
            f"http://127.0.0.1:{GRAFANA_LOCAL_PORT}/api/datasources/uid/"
            "hooklane-prometheus/health"
        )
        if str(datasource_health.get("status", "")).upper() != "OK":
            fail("Grafana datasource health check did not pass")

        public_objects = {
            "targets": targets,
            "dashboard": dashboard,
            "datasource": datasource,
            "datasource_health": datasource_health,
        }
        public_state = json.dumps(public_objects, sort_keys=True)
        if marker in public_state or event_id in public_state:
            fail("payload marker or event ID appeared in observability configuration")
        for forbidden in ("credential", "redis_password", "cookie", "idempotency-key"):
            affected = [
                name
                for name, value in public_objects.items()
                if any(forbidden in item.lower() for item in string_values(value))
            ]
            if affected:
                fail(
                    f"observability output contains forbidden marker {forbidden} "
                    f"in {','.join(affected)}"
                )
        print(
            "[ok] Prometheus targets/metrics/PromQL and Grafana dashboard/datasource passed; "
            f"available API replicas={int(available_api)}, queue depth=0"
        )


def smoke_alerts() -> None:
    required_alerts = {
        "HooklaneApiHighErrorRate",
        "HooklaneQueueBacklogGrowing",
        "HooklaneOldestEventTooOld",
        "HooklaneDeliveryFailureRateHigh",
        "HooklaneRetryRateHigh",
        "HooklaneDeadLetterIncreasing",
        "HooklaneRedisOperationFailures",
    }
    retry_expression = (
        'sum(hooklane_retry_scheduled_total{service="worker"})'
    )
    failure_expression = (
        'sum(hooklane_delivery_outcomes_total{service="worker",'
        'outcome=~"retry_scheduled|dead_lettered|pending|failure"})'
    )
    with port_forward("hooklane-prometheus", PROMETHEUS_LOCAL_PORT, 9090):
        wait_json(f"http://127.0.0.1:{PROMETHEUS_LOCAL_PORT}/api/v1/status/buildinfo")
        loaded = loaded_alert_names()
        if loaded != required_alerts:
            fail(f"loaded alert set does not match the seven-rule contract: {sorted(loaded)}")
        retry_before = query_value(retry_expression)
        failures_before = query_value(failure_expression)
        injected_event_ids: list[str] = []
        try:
            set_mock_sink_mode("server_error")
            injected_event_ids = [
                submit_event(f"failure-injection-{uuid4()}") for _attempt in range(3)
            ]
            wait_for_metric(retry_expression, retry_before + 3, timeout_seconds=45)
            wait_for_metric(failure_expression, failures_before + 3, timeout_seconds=45)
            delivery_state = wait_for_alert(
                "HooklaneDeliveryFailureRateHigh",
                {"pending", "firing"},
                timeout_seconds=45,
            )
            retry_state = wait_for_alert(
                "HooklaneRetryRateHigh",
                {"pending", "firing"},
                timeout_seconds=45,
            )
        finally:
            set_mock_sink_mode("accept")

        if mock_sink_mode() != "accept":
            fail("failure injection was not disabled")
        injected_outcomes = [
            wait_for_terminal_status(event_id, timeout_seconds=75)
            for event_id in injected_event_ids
        ]
        submit_and_wait_for_delivery(f"recovery-{uuid4()}")
        deadline = time.monotonic() + 75
        queue_depth = -1.0
        while time.monotonic() < deadline:
            queue_depth = query_value(
                'max(hooklane_queue_depth{service=~"api|worker"})'
            )
            if queue_depth == 0:
                break
            time.sleep(2)
        if queue_depth != 0:
            fail(f"queue depth did not recover after failure injection: {queue_depth}")
        wait_for_alert(
            "HooklaneDeliveryFailureRateHigh",
            {"inactive"},
            timeout_seconds=75,
        )
        wait_for_alert(
            "HooklaneRetryRateHigh",
            {"inactive"},
            timeout_seconds=75,
        )
        print(
            "[ok] seven alerts loaded; downstream 5xx increased delivery/retry signals, "
            f"alert states were {delivery_state}/{retry_state}, injected outcomes were "
            f"{','.join(sorted(injected_outcomes))}, and recovery returned to inactive"
        )


def smoke() -> None:
    smoke_base()
    smoke_alerts()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("up", "smoke-base", "smoke", "down"))
    return parser.parse_args()


def main() -> int:
    command = parse_args().command
    if command == "up":
        up()
    elif command == "smoke-base":
        smoke_base()
    elif command == "smoke":
        smoke()
    else:
        kind_runtime.cluster_down()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"[fail] observability runtime: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
