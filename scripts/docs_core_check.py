"""Validate Hooklane core documentation against repository contracts."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Never


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ARCHITECTURE = DOCS / "ARCHITECTURE.md"
OPERATIONS = DOCS / "OPERATIONS.md"
SECURITY = DOCS / "SECURITY.md"
LIMITATIONS = DOCS / "LIMITATIONS.md"
SLO = DOCS / "SLO.md"
ADRS = (
    DOCS / "adr" / "0001-redis-streams-at-least-once.md",
    DOCS / "adr" / "0002-health-semantics.md",
    DOCS / "adr" / "0003-local-kind-observability.md",
)
CORE_DOCUMENTS = (ARCHITECTURE, OPERATIONS, SECURITY, LIMITATIONS, SLO, *ADRS)
RUNBOOKS = tuple(sorted((DOCS / "runbooks").glob("*.md")))
INCIDENTS = tuple(sorted((DOCS / "incidents").glob("*.md")))
REFERENCE_DOCUMENTS = (*CORE_DOCUMENTS, *RUNBOOKS, *INCIDENTS)

REQUIRED_SECTIONS: dict[Path, tuple[str, ...]] = {
    ARCHITECTURE: (
        "## System context",
        "## Component responsibilities",
        "## Request-to-delivery data flow",
        "## Event status lifecycle",
        "## Idempotency contract",
        "## Delivery guarantee, retry, and dead-letter",
        "## Pending message recovery",
        "## Health semantics and graceful shutdown",
        "## Deployment topologies",
        "## Observability",
        "## CI structure",
        "## Trust boundaries",
        "## Data retention and persistence",
        "## Failure modes",
        "## Non-goals",
    ),
    OPERATIONS: (
        "## Scope and prerequisites",
        "## Make interface",
        "## Local Compose flow",
        "## Quality, security, and chart gates",
        "## Basic kind and Helm flow",
        "## Observability flow",
        "## Logs, metrics, alerts, and event status",
        "## Alert and Runbook index",
        "## Incident and postmortem index",
        "## Rolling update and rollback",
        "## Common failures and first checks",
        "## Cleanup and hygiene",
    ),
    SECURITY: (
        "## Scope and assurance boundary",
        "## Secret and sensitive-data policy",
        "## Request and log handling",
        "## Metrics and cardinality",
        "## Container hardening",
        "## Kubernetes identity and RBAC",
        "## Network exposure",
        "## Dependency and tool pinning",
        "## Security gates",
        "## GitHub Actions controls",
        "## Failure safety",
        "## Residual risks",
        "## Security reporting",
    ),
    LIMITATIONS: (
        "## Intended use",
        "## Availability and topology",
        "## Delivery semantics",
        "## Production and traffic evidence",
        "## Observability and incident response",
        "## Security and networking",
        "## CI and release",
        "## Persistence by environment",
        "## Safe interpretation of evidence",
    ),
    SLO: (
        "## この文書の位置づけ",
        "## API受付可用性",
        "## API enqueue latency",
        "## 配送成功率",
        "## 配送適時性",
        "## queue backlogとoldest event age",
        "## error budget",
        "## downstream障害期間の扱い",
        "## local kind測定と本番実績の違い",
        "## AlertとRunbook対応",
        "## Incident drillとの対応",
    ),
}
ADR_SECTIONS = (
    "## Status",
    "## Context",
    "## Decision",
    "## Alternatives considered",
    "## Consequences",
)
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
MAKE_REFERENCE = re.compile(r"\bmake\s+([a-z][a-z0-9_.-]*)\b")
MAKE_TARGET = re.compile(r"^([a-z][a-z0-9_.-]*):", re.MULTILINE)
METRIC_REFERENCE = re.compile(r"\bhooklane_[a-z0-9_]+\b")
METRIC_DEFINITION = re.compile(r'"(hooklane_[a-z0-9_]+)"\s*:')
ALERT_REFERENCE = re.compile(
    r"\bHooklane(?:Api|Queue|Oldest|Delivery|Retry|DeadLetter|Redis)[A-Za-z]+\b"
)
ALERT_DEFINITION = re.compile(r"^\s*-\s*alert:\s*(Hooklane[A-Za-z]+)\s*$", re.MULTILINE)


class DocumentationContractError(RuntimeError):
    """Raised when documentation diverges from a repository contract."""


def fail(message: str) -> Never:
    raise DocumentationContractError(message)


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def validate_required_structure() -> None:
    for document in CORE_DOCUMENTS:
        if not document.is_file():
            fail(f"required document is missing: {relative(document)}")
        text = document.read_text(encoding="utf-8")
        if len(re.findall(r"^# [^#]", text, re.MULTILINE)) != 1:
            fail(f"document must contain exactly one H1: {relative(document)}")

    for document, sections in REQUIRED_SECTIONS.items():
        text = document.read_text(encoding="utf-8")
        for section in sections:
            if section not in text:
                fail(f"required section is missing from {relative(document)}: {section}")

    for adr in ADRS:
        text = adr.read_text(encoding="utf-8")
        for section in ADR_SECTIONS:
            if section not in text:
                fail(f"ADR section is missing from {relative(adr)}: {section}")
        if "Accepted" not in text:
            fail(f"ADR status is not recorded as Accepted: {relative(adr)}")


def validate_local_links() -> None:
    for document in REFERENCE_DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.split("#", 1)[0]
            if raw_target.startswith(("http://", "https://")):
                continue
            resolved = document if not target else (document.parent / target).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                fail(f"local link does not resolve in {relative(document)}: {raw_target}")


def validate_make_targets() -> None:
    makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
    known_targets = set(MAKE_TARGET.findall(makefile_text))
    for document in REFERENCE_DOCUMENTS:
        for target in MAKE_REFERENCE.findall(document.read_text(encoding="utf-8")):
            if target not in known_targets:
                fail(f"unknown Make target in {relative(document)}: {target}")


def validate_metrics_and_alerts() -> None:
    metrics_source = (ROOT / "src" / "hooklane" / "observability" / "metrics.py")
    known_metrics = set(METRIC_DEFINITION.findall(metrics_source.read_text(encoding="utf-8")))
    rules = (
        ROOT
        / "charts"
        / "hooklane"
        / "files"
        / "prometheus"
        / "rules"
        / "hooklane-alerts.yml"
    )
    known_alerts = set(ALERT_DEFINITION.findall(rules.read_text(encoding="utf-8")))
    referenced_alerts: set[str] = set()

    for document in REFERENCE_DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for metric in set(METRIC_REFERENCE.findall(text)):
            base_metric = re.sub(r"_(?:bucket|sum|count)$", "", metric)
            if metric not in known_metrics and base_metric not in known_metrics:
                fail(f"unknown metric in {relative(document)}: {metric}")
        for alert in set(ALERT_REFERENCE.findall(text)):
            referenced_alerts.add(alert)
            if alert not in known_alerts:
                fail(f"unknown alert in {relative(document)}: {alert}")

    missing_alerts = known_alerts - referenced_alerts
    if missing_alerts:
        fail(f"alert is absent from core references: {', '.join(sorted(missing_alerts))}")


def validate_cross_references() -> None:
    architecture = ARCHITECTURE.read_text(encoding="utf-8")
    operations = OPERATIONS.read_text(encoding="utf-8")
    security = SECURITY.read_text(encoding="utf-8")
    limitations = LIMITATIONS.read_text(encoding="utf-8")
    slo = SLO.read_text(encoding="utf-8")

    for adr in ADRS:
        if f"adr/{adr.name}" not in architecture:
            fail(f"Architecture does not link ADR: {adr.name}")
    for runbook in RUNBOOKS:
        if f"runbooks/{runbook.name}" not in operations:
            fail(f"Operations does not link Runbook: {runbook.name}")
    for incident in INCIDENTS:
        if f"incidents/{incident.name}" not in operations:
            fail(f"Operations does not link incident artifact: {incident.name}")
    for core_name in ("ARCHITECTURE.md", "OPERATIONS.md", "SECURITY.md", "LIMITATIONS.md"):
        if core_name not in slo:
            fail(f"SLO does not link core document: {core_name}")
    for policy in (
        "container-policy.json",
        "security-policy.json",
        "toolchain.toml",
        ".github/workflows/ci.yml",
    ):
        if policy not in architecture + security:
            fail(f"security or architecture does not link source policy: {policy}")

    required_limitations = (
        "local / portfolio-scale demonstration",
        "non-production-ready",
        "single node",
        "single instance",
        "automatic failover",
        "single replica",
        "at-least-once",
        "exactly-onceではない",
        "event ID",
        "Long-running load test",
        "multi-zone",
        "External downstream",
        "本番traffic",
        "30日SLO達成実績ではない",
        "Alertmanager",
        "distributed tracing",
        "Autoscaling",
        "NetworkPolicy",
        "GitHub hosted Actions",
        "Scanner database更新",
        "Irreversible database migration",
    )
    for marker in required_limitations:
        if marker not in limitations:
            fail(f"Limitations is missing an explicit constraint: {marker}")


def validate_hygiene() -> None:
    forbidden_claims = (
        "完全に安全",
        "本番対応済み",
        "production secure",
    )
    secret_patterns = (
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    )
    for document in REFERENCE_DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for claim in forbidden_claims:
            if claim.lower() in text.lower():
                fail(f"unsupported security or production claim in {relative(document)}")
        if re.search(r":latest\b", text):
            fail(f"unfixed image recommendation in {relative(document)}")
        if re.search(r"/(?:home|Users)/[^/\s]+/", text) or "C:\\Users\\" in text:
            fail(f"personal absolute path in {relative(document)}")
        if any(pattern.search(text) for pattern in secret_patterns):
            fail(f"secret-like value in {relative(document)}")


def run_checks() -> None:
    if len(RUNBOOKS) < 7 or len(INCIDENTS) < 4:
        fail("Runbook or incident index is incomplete")
    validate_required_structure()
    validate_local_links()
    validate_make_targets()
    validate_metrics_and_alerts()
    validate_cross_references()
    validate_hygiene()


def main() -> int:
    try:
        run_checks()
    except (OSError, DocumentationContractError) as error:
        print(f"[fail] docs core contract: {error}")
        return 1
    print(
        "[ok] core docs, ADRs, links, Make targets, alerts, metrics, "
        "Runbooks, incidents, and hygiene passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
