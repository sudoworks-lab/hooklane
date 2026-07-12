"""Validate public entrypoint and demonstration documentation."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Never

import docs_core_check
import repository_hygiene


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DEMO = ROOT / "docs" / "DEMO.md"
RELEASE_EVIDENCE = ROOT / "docs" / "RELEASE_EVIDENCE.md"
THIRD_PARTY_NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
LICENSE = ROOT / "LICENSE"
DOCUMENTS = (README, DEMO, RELEASE_EVIDENCE, THIRD_PARTY_NOTICES)
README_SECTIONS = (
    "## Quick start",
    "## Source distribution",
    "## Problem and approach",
    "## Architecture overview",
    "## Key guarantees",
    "## Non-guarantees",
    "## Local Compose demo",
    "## kind and Helm demo",
    "## Observability",
    "## Rolling update and rollback",
    "## Incident drills",
    "## Quality and security",
    "## Clean-room verification",
    "## CI",
    "## Goal Loop development runner",
    "## Documentation",
    "## Cleanup",
    "## Project status",
    "## License and third-party notices",
)
DEMO_SECTIONS = tuple(f"## {number}. {title}" for number, title in (
    (1, "Prerequisites"),
    (2, "Repository initialization"),
    (3, "Compose quick demo"),
    (4, "Event acceptance"),
    (5, "Delivered status"),
    (6, "Idempotency"),
    (7, "Metrics and dashboard"),
    (8, "kind deploy"),
    (9, "Rolling update"),
    (10, "Bad release and rollback"),
    (11, "Incident drills"),
    (12, "Cleanup"),
    (13, "Expected evidence"),
    (14, "Known limitations"),
))
EVIDENCE_CATEGORIES = (
    "### Architecture",
    "### Failure modes",
    "### Health semantics",
    "### Verification results",
    "### Constraints",
)
RELEASE_EVIDENCE_SECTIONS = (
    "## Scope",
    "## Feature acceptance",
    "## Quality gate",
    "## Runtime verification",
    "## Security scanning",
    "## Verified facts",
    "## Not verified",
    "## Distribution boundary",
)
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
MAKE_REFERENCE = re.compile(r"\bmake\s+([a-z][a-z0-9_.-]*)\b")
MAKE_TARGET = re.compile(r"^([a-z][a-z0-9_.-]*):", re.MULTILINE)
SCRIPT_REFERENCE = re.compile(r"\bbash\s+(scripts/[a-zA-Z0-9_.-]+)")
METRIC_REFERENCE = re.compile(r"\bhooklane_[a-z0-9_]+\b")
METRIC_DEFINITION = re.compile(r'"(hooklane_[a-z0-9_]+)"\s*:')


class DocsCheckError(RuntimeError):
    """Raised when README or DEMO diverges from the repository."""


def fail(message: str) -> Never:
    raise DocsCheckError(message)


def validate_structure() -> None:
    for document in DOCUMENTS:
        if not document.is_file():
            fail(f"required public document is missing: {document.relative_to(ROOT)}")
        text = document.read_text(encoding="utf-8")
        if len(re.findall(r"^# [^#]", text, re.MULTILINE)) != 1:
            fail(f"document must contain exactly one H1: {document.relative_to(ROOT)}")

    readme = README.read_text(encoding="utf-8")
    demo = DEMO.read_text(encoding="utf-8")
    for section in README_SECTIONS:
        if section not in readme:
            fail(f"README section is missing: {section}")
    for section in DEMO_SECTIONS:
        if section not in demo:
            fail(f"DEMO section is missing: {section}")
    for category in EVIDENCE_CATEGORIES:
        if category not in demo:
            fail(f"DEMO evidence category is missing: {category}")
    evidence = RELEASE_EVIDENCE.read_text(encoding="utf-8")
    for section in RELEASE_EVIDENCE_SECTIONS:
        if section not in evidence:
            fail(f"release evidence section is missing: {section}")


def validate_links_and_commands() -> None:
    makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
    known_targets = set(MAKE_TARGET.findall(makefile_text))
    metrics_source = ROOT / "src" / "hooklane" / "observability" / "metrics.py"
    known_metrics = set(
        METRIC_DEFINITION.findall(metrics_source.read_text(encoding="utf-8"))
    )

    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.split("#", 1)[0]
            if raw_target.startswith(("http://", "https://")):
                continue
            resolved = document if not target else (document.parent / target).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                fail(
                    f"local link does not resolve in {document.relative_to(ROOT)}: "
                    f"{raw_target}"
                )
        for target in MAKE_REFERENCE.findall(text):
            if target not in known_targets:
                fail(f"unknown Make target in {document.relative_to(ROOT)}: {target}")
        for script in SCRIPT_REFERENCE.findall(text):
            if not (ROOT / script).is_file():
                fail(f"unknown script in {document.relative_to(ROOT)}: {script}")
        for metric in set(METRIC_REFERENCE.findall(text)):
            base_metric = re.sub(r"_(?:bucket|sum|count)$", "", metric)
            if metric not in known_metrics and base_metric not in known_metrics:
                fail(f"unknown metric in {document.relative_to(ROOT)}: {metric}")


def validate_readme_contract() -> None:
    readme = README.read_text(encoding="utf-8")
    required = (
        "local demonstration",
        "non-production-ready",
        "at-least-once",
        "event IDをdeduplication key",
        "Exactly-once deliveryではない",
        "make demo-smoke",
        "make e2e-kind",
        "make rollout-smoke",
        "make observability-smoke",
        "make incident-smoke",
        "make verify",
        "make clean-room",
        "GitHub hosted Actions上の実行は未確認",
        "MIT License",
        "source-only distribution",
        "Prebuilt container image",
        "docs/RELEASE_EVIDENCE.md",
        "THIRD_PARTY_NOTICES.md",
        "requirements.lock",
        "Third-party sourceまたはbinaryはvendoredせず",
    )
    for marker in required:
        if marker not in readme:
            fail(f"README contract marker is missing: {marker}")
    for prohibited in ("転職", "採用担当", "ポートフォリオ"):
        if prohibited in readme:
            fail(f"README contains an internal or unsupported claim: {prohibited}")
    if re.search(r"(?<!non-)production-readyである", readme):
        fail("README contains an unsupported production-ready claim")
    if re.search(r"!\[[^\]]*\]\([^)]*github", readme, re.IGNORECASE):
        fail("README must not guess a remote CI badge URL")
    if re.search(r"github\.com/[^/\s]+/[^/\s]+", readme):
        fail("README must not guess a remote repository owner")
    if re.search(r"\b\d+/29\b", readme):
        fail("README must not freeze a volatile feature count")
    for obsolete in ("License未選定", "利用許諾と解釈しない"):
        if obsolete in readme:
            fail(f"README retains obsolete license state: {obsolete}")


def validate_demo_contract() -> None:
    demo = DEMO.read_text(encoding="utf-8")
    required = (
        "202 Accepted",
        "delivered",
        "Idempotency-Key",
        "hooklane_http_requests_total",
        "hooklane_queue_depth",
        "hooklane_pending_messages",
        "Bad release",
        "Downstream 5xx",
        "Redis outage",
        "Worker stop",
        "accepted event loss 0",
        "at-least-once",
        "event-ID deduplication",
        "make runtime-hygiene-check",
    )
    for marker in required:
        if marker not in demo:
            fail(f"DEMO contract marker is missing: {marker}")
    if "固定時間は保証しない" not in demo:
        fail("DEMO must not promise a fixed execution duration")
    for marker in (
        "source-only",
        "prebuilt container image",
        "MIT License",
        "THIRD_PARTY_NOTICES.md",
        "production readiness",
    ):
        if marker not in demo:
            fail(f"DEMO distribution marker is missing: {marker}")


def validate_release_evidence_contract() -> None:
    evidence = RELEASE_EVIDENCE.read_text(encoding="utf-8")
    for marker in (
        "F001 through F029",
        "29/29",
        "Blocked feature count is 0",
        "make verify",
        "make demo-smoke",
        "make e2e-kind",
        "make rollout-smoke",
        "make observability-smoke",
        "make incident-smoke",
        "make clean-room",
        "Gitleaks",
        "OSV-Scanner",
        "Trivy",
        "GitHub hosted Actions has not been executed",
        "not a claim of production readiness",
        "source-only distribution",
    ):
        if marker not in evidence:
            fail(f"release evidence marker is missing: {marker}")

    total, passed, blocked = repository_hygiene.feature_state()
    if (total, passed, blocked) != (29, 29, 0):
        fail("release evidence disagrees with feature state")


def validate_license_and_notices_contract() -> None:
    if LICENSE.read_text(encoding="utf-8") != repository_hygiene.EXPECTED_MIT_LICENSE:
        fail("LICENSE does not match the approved MIT contract")
    notices = THIRD_PARTY_NOTICES.read_text(encoding="utf-8")
    for section in (
        "## 1. Scope",
        "## 2. No vendored third-party source",
        "## 3. Python dependencies",
        "## 4. Container base and runtime images",
        "## 5. Development and validation tools",
        "## 6. GitHub Actions",
        "## 7. Distribution note",
        "## 8. How to review exact versions",
    ):
        if section not in notices:
            fail(f"third-party notices section is missing: {section}")


def validate_hygiene() -> None:
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        if re.search(r":latest\b", text):
            fail(f"document recommends an unfixed image: {document.relative_to(ROOT)}")
        if re.search(r"/(?:home|Users)/[^/\s]+/", text):
            fail(f"document contains a personal absolute path: {document.relative_to(ROOT)}")
        if re.search(r"/mnt/[a-zA-Z]/Users/", text):
            fail(f"document contains a WSL-specific path: {document.relative_to(ROOT)}")
        if re.search(r"\b(?:TODO|FIXME|XXX)\b", text):
            fail(f"document contains an unresolved marker: {document.relative_to(ROOT)}")

    readme = README.read_text(encoding="utf-8")
    if "MIT License" not in readme:
        fail("README does not identify the approved MIT license")


def run_checks() -> None:
    docs_core_check.run_checks()
    validate_structure()
    validate_links_and_commands()
    validate_readme_contract()
    validate_demo_contract()
    validate_release_evidence_contract()
    validate_license_and_notices_contract()
    validate_hygiene()


def main() -> int:
    try:
        run_checks()
    except (OSError, DocsCheckError, docs_core_check.DocumentationContractError) as error:
        print(f"[fail] docs contract: {error}")
        return 1
    print(
        "[ok] README, DEMO, release evidence, third-party notices, MIT license, "
        "links, commands, metrics, source-only, and public-claim contract passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
