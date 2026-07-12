"""Validate the F017 chart, dashboard, image pins, and PromQL contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
from typing import Any, Never, cast

from chart_validate_base import CHART, KUBE_VERSION, rendered_resources, run_helm


ROOT = Path(__file__).resolve().parents[1]
PROMETHEUS_IMAGE = (
    "prom/prometheus@sha256:"
    "f39df5334dee301b885f77e0ff1159f5d8a43bf9db518f885544594799a1e3c2"
)
GRAFANA_IMAGE = (
    "grafana/grafana@sha256:"
    "5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2"
)
DASHBOARD = CHART / "files" / "grafana" / "dashboards" / "hooklane-overview.json"
SLI_PROMQL = ROOT / "observability" / "sli-promql.json"
METRIC_CONTRACT = ROOT / "src" / "hooklane" / "observability" / "metrics.py"
REQUIRED_SLI = {
    "api_acceptance_success_rate",
    "api_latency_p95",
    "delivery_success_rate",
    "delivery_within_60_seconds",
    "queue_backlog",
    "oldest_event_age",
}
REQUIRED_PANELS = {
    "API request rate",
    "API error rate",
    "API p50 latency",
    "API p95 latency",
    "Enqueue success / failure",
    "Queue depth",
    "Oldest queued event age",
    "Delivery success / failure",
    "Delivery p95 latency",
    "Retry count",
    "Dead-letter count",
    "Pending message count",
    "Redis error",
    "Available API replicas",
    "Worker in-flight",
    "Delivery within 60 seconds",
}
FORBIDDEN_PROMQL_LABELS = {
    "event_id",
    "request_id",
    "idempotency_key",
    "url",
    "payload_type",
    "exception_message",
    "user_input",
}


def fail(message: str) -> Never:
    raise RuntimeError(message)


def load_object(path: Path) -> dict[str, Any]:
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail(f"{path.relative_to(ROOT)} must contain a JSON object")
    return cast(dict[str, Any], data)


def dashboard_expressions(dashboard: dict[str, Any]) -> list[str]:
    panels = dashboard.get("panels")
    if not isinstance(panels, list):
        fail("dashboard panels must be a list")
    expressions: list[str] = []
    for panel_object in panels:
        if not isinstance(panel_object, dict):
            fail("dashboard panel must be an object")
        panel = cast(dict[str, Any], panel_object)
        targets = panel.get("targets")
        if not isinstance(targets, list) or not targets:
            fail(f"dashboard panel {panel.get('title')} has no target")
        for target_object in targets:
            if not isinstance(target_object, dict):
                fail("dashboard target must be an object")
            expression = target_object.get("expr")
            if not isinstance(expression, str) or not expression.strip():
                fail(f"dashboard panel {panel.get('title')} has no PromQL")
            expressions.append(expression)
    return expressions


def validate_promql_contract(expressions: list[str], sli: dict[str, Any]) -> None:
    metric_names = set(
        re.findall(
            r'"(hooklane_[a-zA-Z0-9_:]+)"',
            METRIC_CONTRACT.read_text(encoding="utf-8"),
        )
    )
    for expression in expressions:
        for label in FORBIDDEN_PROMQL_LABELS:
            if re.search(rf"\b{re.escape(label)}\s*=", expression):
                fail(f"dashboard PromQL uses forbidden label {label}")
        referenced = set(re.findall(r"\b(hooklane_[a-zA-Z0-9_:]+)\b", expression))
        for metric in referenced:
            base_metric = re.sub(r"_(bucket|sum|count)$", "", metric)
            if metric not in metric_names and base_metric not in metric_names:
                fail(f"dashboard references unknown metric {metric}")

    if set(sli) != REQUIRED_SLI:
        fail("SLI PromQL mapping does not contain the required six candidates")
    expression_set = set(expressions)
    for name, query in sli.items():
        if not isinstance(query, str) or query not in expression_set:
            fail(f"SLI query {name} is not present in the dashboard")


def extract_prometheus_config(document: str) -> str:
    match = re.search(
        r"^  prometheus\.yml: \|\n(?P<body>(?:^    .*\n?)+)",
        document,
        flags=re.MULTILINE,
    )
    if match is None:
        fail("rendered Prometheus ConfigMap has no prometheus.yml")
    return textwrap.dedent(match.group("body"))


def validate_prometheus_config(config: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="hooklane-prometheus-",
        suffix=".yml",
    ) as config_file:
        config_file.write(config)
        config_file.flush()
        os.chmod(config_file.name, 0o644)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/bin/promtool",
                "--volume",
                f"{config_file.name}:/tmp/prometheus.yml:ro",
                PROMETHEUS_IMAGE,
                "check",
                "config",
                "/tmp/prometheus.yml",
            ],
            cwd=ROOT,
            check=True,
        )


def validate_render() -> None:
    disabled_render = run_helm(
        "template",
        "hooklane",
        str(CHART),
        "--namespace",
        "hooklane",
        "--kube-version",
        KUBE_VERSION,
    )
    disabled_resources = rendered_resources(disabled_render)
    if any(
        name in {"hooklane-prometheus", "hooklane-grafana"}
        for _kind, name in disabled_resources
    ):
        fail("observability resources render while observability.enabled is false")
    run_helm("lint", str(CHART), "--strict", "--set", "observability.enabled=true")
    rendered = run_helm(
        "template",
        "hooklane",
        str(CHART),
        "--namespace",
        "hooklane",
        "--kube-version",
        KUBE_VERSION,
        "--set",
        "observability.enabled=true",
    )
    resources = rendered_resources(rendered)
    required = {
        ("Deployment", "hooklane-prometheus"),
        ("Deployment", "hooklane-grafana"),
        ("Service", "hooklane-prometheus"),
        ("Service", "hooklane-grafana"),
        ("ConfigMap", "hooklane-prometheus-config"),
        ("ConfigMap", "hooklane-grafana-provisioning"),
        ("ConfigMap", "hooklane-grafana-dashboard"),
        ("Role", "hooklane-prometheus-discovery"),
        ("RoleBinding", "hooklane-prometheus-discovery"),
    }
    missing = sorted(required - set(resources))
    if missing:
        fail(f"observability render is missing resources: {missing}")
    if PROMETHEUS_IMAGE not in rendered or GRAFANA_IMAGE not in rendered:
        fail("observability images do not match the approved digests")
    if ":latest" in rendered:
        fail("observability render uses latest")
    for key in (
        ("Deployment", "hooklane-prometheus"),
        ("Deployment", "hooklane-grafana"),
    ):
        workload = resources[key]
        for fragment in (
            "runAsNonRoot: true",
            "allowPrivilegeEscalation: false",
            "readOnlyRootFilesystem: true",
            'drop: ["ALL"]',
            "type: RuntimeDefault",
            "resources:",
            "readinessProbe:",
            "livenessProbe:",
            "mountPath: /tmp",
        ):
            if fragment not in workload:
                fail(f"{key} is missing hardening fragment {fragment}")
    prometheus_config = extract_prometheus_config(
        resources[("ConfigMap", "hooklane-prometheus-config")]
    )
    for fragment in (
        "kubernetes_sd_configs:",
        "__meta_kubernetes_pod_annotation_prometheus_io_scrape",
        "role: pod",
    ):
        if fragment not in prometheus_config:
            fail(f"Prometheus config is missing {fragment}")
    validate_prometheus_config(prometheus_config)


def main() -> int:
    dashboard = load_object(DASHBOARD)
    sli = load_object(SLI_PROMQL)
    panels = cast(list[dict[str, Any]], dashboard.get("panels"))
    titles = {panel.get("title") for panel in panels}
    missing_panels = sorted(REQUIRED_PANELS - titles)
    if dashboard.get("uid") != "hooklane-overview" or missing_panels:
        fail(f"dashboard identity or panels are incomplete: {missing_panels}")
    expressions = dashboard_expressions(dashboard)
    validate_promql_contract(expressions, sli)
    rendered_dashboard = json.dumps(dashboard, sort_keys=True).lower()
    for forbidden in ("payload", "credential", "redis_password", "cookie", "secret"):
        if forbidden in rendered_dashboard:
            fail(f"dashboard contains forbidden content marker {forbidden}")
    validate_render()
    print(
        f"[ok] observability chart, images, dashboard {len(panels)} panels, "
        f"and {len(sli)} SLI PromQL contracts passed"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (json.JSONDecodeError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[fail] observability validation: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
