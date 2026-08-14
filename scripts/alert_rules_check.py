"""Validate Prometheus alert rules and their dashboard, SLO, and Runbook links."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Never, cast

from chart_validate_base import CHART, KUBE_VERSION, rendered_resources, run_helm
from observability_validate import PROMETHEUS_IMAGE


ROOT = Path(__file__).resolve().parents[1]
RULES = CHART / "files" / "prometheus" / "rules" / "hooklane-alerts.yml"
RULE_TESTS = CHART / "files" / "prometheus" / "rules" / "hooklane-alerts.test.yml"
DASHBOARD = CHART / "files" / "grafana" / "dashboards" / "hooklane-overview.json"
SLO = ROOT / "docs" / "SLO.md"
METRIC_CONTRACT = ROOT / "src" / "hooklane" / "observability" / "metrics.py"
REQUIRED_ALERTS = {
    "HooklaneApiHighErrorRate",
    "HooklaneApiUnavailable",
    "HooklaneWorkerUnavailable",
    "HooklaneQueueBacklogGrowing",
    "HooklaneOldestEventTooOld",
    "HooklaneDeliveryFailureRateHigh",
    "HooklaneRetryRateHigh",
    "HooklaneDeadLetterIncreasing",
    "HooklaneRedisOperationFailures",
}
REQUIRED_ANNOTATIONS = {"summary", "impact", "runbook", "dashboard", "slo_sli", "severity"}
REQUIRED_RUNBOOK_HEADINGS = {
    "## 影響",
    "## 確認するdashboard / metric",
    "## 最初の切り分け",
    "## logs / events / status確認",
    "## 直近変更確認",
    "## 暫定対応",
    "## 復旧確認",
    "## escalation条件",
    "## 恒久対策候補",
    "## known limitations",
}


def fail(message: str) -> Never:
    raise RuntimeError(message)


def load_dashboard() -> dict[str, Any]:
    data: object = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail("dashboard must be a JSON object")
    return cast(dict[str, Any], data)


def promtool_check_rules() -> None:
    os.chmod(RULES, 0o644)
    os.chmod(RULE_TESTS, 0o644)
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
            f"{RULES.parent}:/tmp/rules:ro",
            PROMETHEUS_IMAGE,
            "check",
            "rules",
            "/tmp/rules/hooklane-alerts.yml",
        ],
        cwd=ROOT,
        check=True,
    )
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
            f"{RULES.parent}:/tmp/rules:ro",
            PROMETHEUS_IMAGE,
            "test",
            "rules",
            "/tmp/rules/hooklane-alerts.test.yml",
        ],
        cwd=ROOT,
        check=True,
    )


def metric_names() -> set[str]:
    return set(
        re.findall(
            r'"(hooklane_[a-zA-Z0-9_:]+)"',
            METRIC_CONTRACT.read_text(encoding="utf-8"),
        )
    )


def alert_blocks(rules_text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for block in re.split(r"(?=^\s+- alert:)", rules_text, flags=re.MULTILINE):
        match = re.search(r"^\s+- alert:\s+(\S+)\s*$", block, flags=re.MULTILINE)
        if match is not None:
            blocks[match.group(1)] = block
    return blocks


def validate_runbook(path_text: str, alert_name: str, make_targets: set[str]) -> None:
    runbook = ROOT / path_text
    if not runbook.is_file() or runbook.parent != ROOT / "docs" / "runbooks":
        fail(f"{alert_name} runbook path does not resolve under docs/runbooks")
    content = runbook.read_text(encoding="utf-8")
    missing = sorted(REQUIRED_RUNBOOK_HEADINGS - set(content.splitlines()))
    if missing:
        fail(f"{runbook.name} is missing headings: {missing}")
    for target in re.findall(r"^\s*make\s+([a-zA-Z0-9_-]+)", content, flags=re.MULTILINE):
        if target not in make_targets:
            fail(f"{runbook.name} references unknown Make target {target}")
    for marker in ("hooklane-alerts.yml", "../SLO.md", "Hooklane SLI and Operations"):
        if marker not in content:
            fail(f"{runbook.name} does not cross-reference {marker}")


def validate_rule_contracts() -> tuple[set[str], set[str]]:
    rules_text = RULES.read_text(encoding="utf-8")
    blocks = alert_blocks(rules_text)
    if set(blocks) != REQUIRED_ALERTS:
        fail(f"alert rules do not match the nine-rule contract: {sorted(blocks)}")
    known_metrics = metric_names()
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    make_targets = set(re.findall(r"^([a-zA-Z0-9_-]+):", makefile, flags=re.MULTILINE))
    dashboard = load_dashboard()
    panels = dashboard.get("panels")
    if not isinstance(panels, list):
        fail("dashboard panels are missing")
    panel_titles = {
        panel.get("title")
        for panel in panels
        if isinstance(panel, dict) and isinstance(panel.get("title"), str)
    }
    slo_headings = {
        re.sub(r"[^0-9a-zA-Z_\-\u0080-\uffff]", "", heading.lower().replace(" ", "-"))
        for heading in re.findall(r"^##\s+(.+)$", SLO.read_text(encoding="utf-8"), re.MULTILINE)
    }
    runbooks: set[str] = set()
    referenced_metrics: set[str] = set()
    for name, block in blocks.items():
        if "annotations:" not in block:
            fail(f"{name} has no annotations")
        annotations = block.split("annotations:", maxsplit=1)[1]
        annotation_values: dict[str, str] = {}
        for key in REQUIRED_ANNOTATIONS:
            match = re.search(rf"^\s+{key}:\s+(.+)$", annotations, flags=re.MULTILINE)
            if match is None:
                fail(f"{name} is missing annotation {key}")
            annotation_values[key] = match.group(1).strip()
        if f"severity: {annotation_values['severity']}" not in block.split(
            "annotations:", maxsplit=1
        )[0]:
            fail(f"{name} label and annotation severity do not match")
        label_section = block.split("labels:", maxsplit=1)[1].split(
            "annotations:", maxsplit=1
        )[0]
        label_names = set(
            re.findall(r"^\s+([a-zA-Z_][a-zA-Z0-9_]*):", label_section, re.MULTILINE)
        )
        if label_names != {"severity"}:
            fail(f"{name} alert labels must contain only bounded severity")
        runbook_path = annotation_values["runbook"]
        validate_runbook(runbook_path, name, make_targets)
        runbooks.add(runbook_path)
        dashboard_hint = annotation_values["dashboard"]
        if not any(str(title) in dashboard_hint for title in panel_titles):
            fail(f"{name} dashboard hint does not name an existing panel")
        slo_reference = annotation_values["slo_sli"]
        if not slo_reference.startswith("docs/SLO.md#"):
            fail(f"{name} SLO/SLI reference is not repository-local")
        if slo_reference.split("#", maxsplit=1)[1] not in slo_headings:
            fail(f"{name} SLO/SLI anchor does not resolve")
        metrics = set(re.findall(r"\b(hooklane_[a-zA-Z0-9_:]+)\b", block))
        for metric in metrics:
            base_metric = re.sub(r"_(bucket|sum|count)$", "", metric)
            if metric not in known_metrics and base_metric not in known_metrics:
                fail(f"{name} references unknown metric {metric}")
        referenced_metrics.update(metrics)

        if name in {"HooklaneApiUnavailable", "HooklaneWorkerUnavailable"}:
            expression = block.split("for:", maxsplit=1)[0]
            component = "api" if name == "HooklaneApiUnavailable" else "worker"
            for marker in (
                f'up{{job="hooklane-applications",component="{component}"}}',
                f'hooklane_service_ready{{service="{component}"}}',
                "absent(up{",
                "absent(hooklane_service_ready{",
            ):
                if marker not in expression:
                    fail(f"{name} does not cover required availability signal {marker}")

    required_signals = {
        "hooklane_delivery_outcomes_total",
        "hooklane_retry_scheduled_total",
        "hooklane_dead_letter_total",
    }
    if not required_signals.issubset(referenced_metrics) or not (
        {"hooklane_redis_operation_failures_total", "hooklane_queue_depth"}
        & referenced_metrics
    ):
        fail("alert set does not cover delivery, retry, dead-letter, and Redis/queue signals")
    return set(blocks), runbooks


def validate_cross_links(alerts: set[str], runbooks: set[str]) -> None:
    slo_text = SLO.read_text(encoding="utf-8")
    if "hooklane-overview.json" not in slo_text or "observability/sli-promql.json" not in slo_text:
        fail("SLO does not reference dashboard and SLI PromQL mapping")
    for alert in alerts:
        if alert not in slo_text:
            fail(f"SLO does not reference alert {alert}")
    for runbook in runbooks:
        relative = runbook.removeprefix("docs/")
        if relative not in slo_text:
            fail(f"SLO does not reference {runbook}")

    dashboard = load_dashboard()
    links = dashboard.get("links")
    if not isinstance(links, list):
        fail("dashboard links are missing")
    urls = {link.get("url") for link in links if isinstance(link, dict)}
    if not {"docs/SLO.md", "docs/runbooks/"}.issubset(urls):
        fail("dashboard does not link to SLO and Runbooks")

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
    rules_config = resources.get(("ConfigMap", "hooklane-prometheus-rules"), "")
    prometheus_config = resources.get(("ConfigMap", "hooklane-prometheus-config"), "")
    deployment = resources.get(("Deployment", "hooklane-prometheus"), "")
    if not rules_config or "/etc/prometheus/rules/*.yml" not in prometheus_config:
        fail("rendered Prometheus rule ConfigMap or rule_files entry is missing")
    if "mountPath: /etc/prometheus/rules" not in deployment:
        fail("Prometheus Deployment does not mount alert rules")


def validate_no_sensitive_values() -> None:
    paths = [RULES, SLO, *sorted((ROOT / "docs" / "runbooks").glob("*.md"))]
    content = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    patterns = (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"(?i)password\s*[:=]\s*[^\s`]+",
        r"(?i)redis://[^\s`]*@",
    )
    for pattern in patterns:
        if re.search(pattern, content):
            fail("alert or documentation contains a secret-like value")


def main() -> int:
    promtool_check_rules()
    alerts, runbooks = validate_rule_contracts()
    validate_cross_links(alerts, runbooks)
    validate_no_sensitive_values()
    print(
        f"[ok] {len(alerts)} alert rules, {len(runbooks)} Runbooks, "
        "PromQL metrics, SLO/dashboard links, and sensitive-value checks passed"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (json.JSONDecodeError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[fail] alert rules: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
