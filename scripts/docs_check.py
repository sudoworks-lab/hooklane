"""Validate public documentation, metadata, and release-note contracts."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Never

import docs_core_check
import repository_hygiene


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DEMO = ROOT / "docs" / "DEMO.md"
DEVELOPMENT = ROOT / "docs" / "DEVELOPMENT.md"
RELEASE_EVIDENCE = ROOT / "docs" / "RELEASE_EVIDENCE.md"
AWS_RUNTIME_EVIDENCE = ROOT / "docs" / "aws" / "runtime-evidence.json"
F004_MIGRATION_ADR = ROOT / "docs" / "adr" / "0004-f004-destination-contract-migration.md"
RELEASE_NOTES = ROOT / "docs" / "releases" / "v0.1.1.md"
THIRD_PARTY_NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
LICENSE = ROOT / "LICENSE"
PYPROJECT = ROOT / "pyproject.toml"
CHART = ROOT / "charts" / "hooklane" / "Chart.yaml"
DOCUMENTS = (
    README,
    DEMO,
    DEVELOPMENT,
    RELEASE_EVIDENCE,
    RELEASE_NOTES,
    THIRD_PARTY_NOTICES,
)
README_SECTIONS = (
    "## 主な機能",
    "## アーキテクチャ概要",
    "## 障害時の動作",
    "## 検証済みの範囲",
    "## Quick start",
    "## 設計上の保証",
    "## 保証しないこと・制約",
    "## Docker Compose",
    "## kind / Helm",
    "## 監視",
    "## rolling update / rollback",
    "## incident drill",
    "## quality / security",
    "## GitHub Actions",
    "## 詳細文書",
    "## cleanup",
    "## 配布範囲",
    "## License / third-party notices",
)
DEMO_SECTIONS = tuple(
    f"## {number}. {title}"
    for number, title in (
        (1, "前提"),
        (2, "repository初期化"),
        (3, "Compose quick demo"),
        (4, "event受付"),
        (5, "delivered status"),
        (6, "idempotency"),
        (7, "metricsとdashboard"),
        (8, "kind deploy"),
        (9, "rolling update"),
        (10, "bad releaseとrollback"),
        (11, "incident drill"),
        (12, "cleanup"),
        (13, "確認内容"),
        (14, "既知の制約"),
    )
)
EVIDENCE_CATEGORIES = (
    "### アーキテクチャ",
    "### 障害時の動作",
    "### healthの意味",
    "### 検証結果",
    "### 制約",
)
RELEASE_EVIDENCE_SECTIONS = (
    "## 対象",
    "## feature受け入れ",
    "## quality gate",
    "## runtime検証",
    "## GitHub Actions",
    "## security scan",
    "## 実証済みの事実",
    "## 未確認事項",
    "## 配布範囲",
)
RELEASE_NOTE_SECTIONS = (
    "## 概要",
    "## READMEと文書構成の整理",
    "## 古いCI・公開状態の記載修正",
    "## Goal Loop開発文書の分離",
    "## package / repository metadata",
    "## application behavior",
    "## 検証結果",
    "## 既知の制約",
)
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
MAKE_REFERENCE = re.compile(r"\bmake\s+([a-z][a-z0-9_.-]*)\b")
MAKE_TARGET = re.compile(r"^([a-z][a-z0-9_.-]*):", re.MULTILINE)
SCRIPT_REFERENCE = re.compile(r"\bbash\s+(scripts/[a-zA-Z0-9_.-]+)")
METRIC_REFERENCE = re.compile(r"\bhooklane_[a-z0-9_]+\b")
METRIC_DEFINITION = re.compile(r'"(hooklane_[a-z0-9_]+)"\s*:')


class DocsCheckError(RuntimeError):
    """Raised when public documentation diverges from the repository."""


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

    notes = RELEASE_NOTES.read_text(encoding="utf-8")
    for section in RELEASE_NOTE_SECTIONS:
        if section not in notes:
            fail(f"v0.1.1 release-notes section is missing: {section}")


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
        "Webhook配送基盤",
        "at-least-once",
        "event IDを重複排除キー",
        "Exactly-once deliveryは提供しない",
        "make demo-smoke",
        "make e2e-kind",
        "make rollout-smoke",
        "make observability-smoke",
        "make incident-smoke",
        "make verify",
        "make clean-room",
        "GitHub hosted Actions",
        "v0.1.1のtagがcurrent source baseline",
        "MIT License",
        "source-only",
        "prebuilt container image",
        "docs/RELEASE_EVIDENCE.md",
        "docs/DEVELOPMENT.md",
        "THIRD_PARTY_NOTICES.md",
        "requirements.lock",
    )
    for marker in required:
        if marker not in readme:
            fail(f"README contract marker is missing: {marker}")


def validate_demo_contract() -> None:
    demo = DEMO.read_text(encoding="utf-8")
    required = (
        "202 Accepted",
        "delivered",
        "Idempotency-Key",
        "hooklane_http_requests_total",
        "hooklane_queue_depth",
        "hooklane_pending_messages",
        "bad release",
        "downstream 5xx",
        "Redis outage",
        "worker stop",
        "accepted event loss 0",
        "at-least-once",
        "event ID重複排除",
        "make runtime-hygiene-check",
        "GitHub hosted Actions",
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
    ):
        if marker not in demo:
            fail(f"DEMO distribution marker is missing: {marker}")


def validate_release_evidence_contract() -> None:
    evidence = RELEASE_EVIDENCE.read_text(encoding="utf-8")
    for marker in (
        "F001〜F029",
        "29/29",
        "blocked feature countは0",
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
        "GitHub hosted Actionsは公開mainの旧baselineで実行済み",
        "Quality, security, and chart gatesはsuccess",
        "kind delivery and recovery E2Eはsuccess",
        "source-only",
    ):
        if marker not in evidence:
            fail(f"release evidence marker is missing: {marker}")

    total, passed, blocked = repository_hygiene.feature_state()
    if (total, passed, blocked) != (29, 29, 0):
        fail("release evidence disagrees with feature state")


def validate_current_aws_evidence_contract() -> None:
    if not AWS_RUNTIME_EVIDENCE.is_file():
        fail("sanitized AWS runtime evidence is missing")
    try:
        evidence: object = json.loads(AWS_RUNTIME_EVIDENCE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        fail("sanitized AWS runtime evidence is not valid JSON")
    if not isinstance(evidence, dict):
        fail("sanitized AWS runtime evidence must be a JSON object")
    required = (
        "schema_version",
        "run_id",
        "source_run_id",
        "cleanup_recovery_run_id",
        "source_commit",
        "image_source_commit",
        "image_tag",
        "verdict",
        "iteration",
        "image_digests",
        "region",
        "foundation_plan",
        "foundation_apply",
        "foundation_readiness",
        "runtime_plan",
        "runtime_apply",
        "runtime_health",
        "scenario_outcomes",
        "image_proof_mode",
        "cleanup_plan",
        "cleanup_completion",
        "final_state",
        "retained_resources",
        "verified",
        "unverified",
        "evidence_sources",
        "secret_free",
    )
    for key in required:
        if key not in evidence:
            fail(f"sanitized AWS runtime evidence is missing: {key}")
    if evidence.get("schema_version") != "1.1":
        fail("sanitized AWS runtime evidence schema version is unexpected")
    if evidence.get("secret_free") is not True:
        fail("sanitized AWS runtime evidence must declare secret_free=true")
    if evidence.get("run_id") != "20260802T160316Z":
        fail("sanitized AWS runtime evidence run ID is unexpected")
    if evidence.get("source_run_id") != "20260802T154822Z":
        fail("sanitized AWS runtime evidence source run ID is unexpected")
    if evidence.get("cleanup_recovery_run_id") != "20260802T160316Z":
        fail("sanitized AWS runtime evidence cleanup recovery run ID is unexpected")
    if evidence.get("source_commit") != "123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b":
        fail("sanitized AWS runtime evidence source commit is unexpected")
    if evidence.get("image_source_commit") != "123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b":
        fail("sanitized AWS runtime evidence image source commit is unexpected")
    if evidence.get("image_tag") != "git-123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b":
        fail("sanitized AWS runtime evidence image tag is unexpected")
    if evidence.get("verdict") != "PASS_AND_CLEAN":
        fail("sanitized AWS runtime evidence verdict is unexpected")
    if evidence.get("iteration") != 2:
        fail("sanitized AWS runtime evidence iteration is unexpected")
    if evidence.get("foundation_plan") != {"create": 49, "update": 0, "delete": 0}:
        fail("sanitized AWS runtime evidence has an unexpected foundation plan")
    if evidence.get("foundation_apply") != "PASS" or evidence.get("foundation_readiness") != "PASS":
        fail("sanitized AWS runtime evidence foundation result is unexpected")
    runtime_plan = evidence.get("runtime_plan")
    cleanup_plan = evidence.get("cleanup_plan")
    if runtime_plan != {"create": 0, "update": 3, "delete": 0}:
        fail("sanitized AWS runtime evidence has an unexpected runtime plan")
    if evidence.get("runtime_apply") != "PASS" or evidence.get("runtime_health") != "PASS":
        fail("sanitized AWS runtime evidence runtime result is unexpected")
    if cleanup_plan != {"create": 0, "update": 0, "delete": 49}:
        fail("sanitized AWS runtime evidence has an unexpected cleanup plan")
    scenario = evidence.get("scenario_outcomes")
    if not isinstance(scenario, dict) or any(
        scenario.get(key) is not True
        for key in ("normal_delivery", "idempotency_same", "idempotency_conflict", "smoke_contract")
    ) or scenario.get("smoke_checks") != 4 or scenario.get("smoke_passed") != 4:
        fail("sanitized AWS runtime evidence smoke result is unexpected")
    if evidence.get("image_proof_mode") != {
        "api": "configuration_backed",
        "worker": "configuration_backed",
        "mock_sink": "direct_plan",
    }:
        fail("sanitized AWS runtime evidence image proof mode is unexpected")
    final_state = evidence.get("final_state")
    if not isinstance(final_state, dict) or final_state.get("resource_count") != 6:
        fail("sanitized AWS runtime evidence final state count is unexpected")
    if final_state.get("resource_classes") != {
        "ECR repository": 3,
        "ECR lifecycle policy": 3,
    }:
        fail("sanitized AWS runtime evidence resource classes are unexpected")
    if final_state.get("charge_heavy_resource_count") != 0:
        fail("sanitized AWS runtime evidence charge-heavy count is unexpected")
    if final_state.get("required_images") != 3:
        fail("sanitized AWS runtime evidence required image count is unexpected")
    if final_state.get("ecr_digest_match") is not True:
        fail("sanitized AWS runtime evidence ECR digest match is unexpected")
    if final_state.get("ecr_contract_match") is not True:
        fail("sanitized AWS runtime evidence ECR contract match is unexpected")
    if (
        final_state.get("ecs_service_count") != 0
        or final_state.get("ecs_running_task_count") != 0
        or final_state.get("ecs_pending_task_count") != 0
        or final_state.get("ecs_cluster_state") != "inactive_tombstone"
        or final_state.get("ecs_inactive_tombstone_count") != 1
    ):
        fail("sanitized AWS runtime evidence ECS final state is unexpected")
    if final_state.get("apply_process") != "terminated":
        fail("sanitized AWS runtime evidence apply process is unexpected")
    if evidence.get("cleanup_completion") != "PASS":
        fail("sanitized AWS runtime evidence cleanup completion is unexpected")
    sources = evidence.get("evidence_sources")
    expected_sources = {
        "main_run_receipt_sha256": "cf8d5090f5635e7e7b957f41d65658c3bc6b8ba215781961e4436f46ed06516b",
        "main_run_progress_sha256": "bb6626a6da4909fd121515afb791e86d0394b12390aece13f3bedf0b5768fd0d",
        "main_run_diagnostic_sha256": "b4f806f33461cbe453d79c871c211601f2b5c760b4bd42de1fdae0027f7a08cc",
        "recovery_run_receipt_sha256": "a1aa6f342f6b052525feba59afc6bef961b11b58b82804a37f6e34c3d305922e",
        "recovery_run_progress_sha256": "c183352ccb0e47579ae143d5932e6c7be7100b670e59fe470fd853e31493a746",
        "recovery_run_diagnostic_sha256": "27b8d08b4c13af84090b7927d2d86a8fc6acce243e6bf7e4153827aadedaa4bd",
        "canonical_evidence_sha256": "3d1d6a11d9c64d2272ee2cdfc5027c007300c5dc4ff8cd4c5b229fcb1dd56085",
        "canonical_manifest_sha256": "5ab00c7df3cfef2e1e1fa43982eb09af76a44508e337858978b8ff953d6a74bd",
    }
    if not isinstance(sources, dict) or any(sources.get(key) != value for key, value in expected_sources.items()):
        fail("sanitized AWS runtime evidence artifact SHA provenance is unexpected")

    current_state_documents = (
        README,
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "SECURITY.md",
        ROOT / "docs" / "LIMITATIONS.md",
        RELEASE_EVIDENCE,
        ROOT / "infra" / "README.md",
    )
    scope_markers = (
        "123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b",
        "git-123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b",
        "20260802T160316Z",
        "PASS_AND_CLEAN",
        "foundation 49/0/0",
        "runtime 0/3/0",
        "cleanup 0/0/49",
        "smoke 4/4",
        "image proofはAPI/workerが`configuration_backed`、mock-sinkが`direct_plan`",
        "configuration_backed",
        "direct_plan",
        "final state 6",
        "charge-heavy 0",
        "ECR repository 3",
        "ECS service/task 0",
        "INACTIVE tombstone",
        "apply process terminated",
    )
    for document in current_state_documents:
        text = document.read_text(encoding="utf-8")
        for marker in scope_markers:
            if marker not in text:
                fail(
                    "current-state documentation is missing AWS evidence scope: "
                    f"{document.relative_to(ROOT)}"
                )
    github_scope_documents = (
        README,
        ROOT / "infra" / "README.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "LIMITATIONS.md",
        RELEASE_EVIDENCE,
    )
    for document in github_scope_documents:
        text = document.read_text(encoding="utf-8")
        if "公開mainの旧baseline" not in text or "Push後のPR CI" not in text:
            fail(
                "current-state documentation is missing hosted-CI generation scope: "
                f"{document.relative_to(ROOT)}"
            )
    if not F004_MIGRATION_ADR.is_file():
        fail("F004 contract migration ADR is missing")
    migration = F004_MIGRATION_ADR.read_text(encoding="utf-8")
    for marker in (
        "Human Decision",
        "DELIVERY_TARGET_ALLOWLIST",
        "operator-controlledなstartup configuration",
        "requestから配送先を変更できない",
        "features.json",
        "恒久的な自動編集権限の緩和は行わない",
    ):
        if marker not in migration:
            fail(f"F004 migration ADR is missing: {marker}")
    stale_claims = (
        "修正後のAWS runtimeは未実証",
        "runtime AWS apply、ECS task secret injectionの実行",
        "runtime applyは実行していない",
        "runtimeのhealthy delivery verificationはworker health failureにより未完了",
        "現在HEADおよび新immutable imageはAWS再検証前",
        "現在HEADのAWS実証とは扱わない",
    )
    for document in current_state_documents:
        text = document.read_text(encoding="utf-8")
        for claim in stale_claims:
            if claim in text:
                fail(
                    "current-state documentation retains a stale AWS failure claim: "
                    f"{document.relative_to(ROOT)}"
                )


def validate_metadata_contract() -> None:
    project = PYPROJECT.read_text(encoding="utf-8")
    chart = CHART.read_text(encoding="utf-8")
    if 'version = "0.1.1"' not in project:
        fail("package version differs from the current v0.1.1 baseline")
    if 'description = "Webhook delivery service with retry, recovery, and observability"' not in project:
        fail("package description is missing or stale")
    if "description: Webhook delivery service chart" not in chart:
        fail("chart description is missing or stale")


def validate_license_and_notices_contract() -> None:
    if LICENSE.read_text(encoding="utf-8") != repository_hygiene.EXPECTED_MIT_LICENSE:
        fail("LICENSE does not match the approved MIT contract")
    notices = THIRD_PARTY_NOTICES.read_text(encoding="utf-8")
    for section in (
        "## 対象",
        "## vendored third-party sourceなし",
        "## Python dependency",
        "## container baseとruntime image",
        "## 開発と検証tool",
        "## GitHub Actions",
        "## 配布に関する注記",
        "## exact versionの確認方法",
    ):
        if section not in notices:
            fail(f"third-party notices section is missing: {section}")


def validate_public_claims() -> None:
    prohibited = (
        "portfolio-scale",
        "reference implementation",
        "local demonstration",
        "reliability lab",
        "learning project",
        "training project",
        "toy project",
        "sample project",
        "non-production-ready",
        "github hosted actions has not been executed",
        "github hosted actions未確認",
        "github hosted actions未実行",
        "remote未設定",
        "owner未確定",
        "license未選定",
        "tag未作成",
        "github release未作成",
        "初回公開版",
        "練習",
        "学習用",
        "作ってみた",
        "転職",
        "就職",
        "採用担当",
        "ポートフォリオ",
    )
    for document in (*DOCUMENTS, *docs_core_check.REFERENCE_DOCUMENTS):
        text = document.read_text(encoding="utf-8")
        normalized = text.lower()
        for phrase in prohibited:
            if phrase.lower() in normalized:
                fail(
                    "public document contains a prohibited or stale phrase: "
                    f"{document.relative_to(ROOT)}"
                )


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


def run_checks() -> None:
    docs_core_check.run_checks()
    validate_structure()
    validate_links_and_commands()
    validate_readme_contract()
    validate_demo_contract()
    validate_release_evidence_contract()
    validate_current_aws_evidence_contract()
    validate_metadata_contract()
    validate_license_and_notices_contract()
    validate_public_claims()
    validate_hygiene()


def main() -> int:
    try:
        run_checks()
    except (OSError, DocsCheckError, docs_core_check.DocumentationContractError) as error:
        print(f"[fail] docs contract: {error}")
        return 1
    print(
        "[ok] public docs, release notes, metadata, MIT license, links, commands, "
        "metrics, source-only boundary, and current-state contract passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
