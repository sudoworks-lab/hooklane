"""Unit coverage for final public documentation and repository hygiene."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
POST_MERGE_SNAPSHOT_FILES = (
    "README.md",
    "docs/GOAL.md",
    "docs/ARCHITECTURE.md",
    "docs/LIMITATIONS.md",
    "docs/RELEASE_EVIDENCE.md",
    "docs/SECURITY.md",
    "infra/README.md",
    "scripts/docs_check.py",
    "scripts/repository_hygiene.py",
    "docs/adr/0005-aws-scope-extension.md",
)


def _worktree_snapshot(destination: Path) -> Path:
    snapshot = destination / "snapshot"
    snapshot.mkdir()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(snapshot, filter="data")
    for relative in POST_MERGE_SNAPSHOT_FILES:
        source = ROOT / relative
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return snapshot


def _run_repository_hygiene_release_contract(
    snapshot: Path,
) -> subprocess.CompletedProcess[str]:
    code = """
from pathlib import Path
import repository_hygiene

root = Path.cwd()
repository_hygiene.README = root / "README.md"
repository_hygiene.RELEASE_EVIDENCE = root / "docs" / "RELEASE_EVIDENCE.md"
repository_hygiene.RELEASE_NOTES = root / "docs" / "releases" / "v0.1.1.md"
repository_hygiene.THIRD_PARTY_NOTICES = root / "THIRD_PARTY_NOTICES.md"
repository_hygiene.LICENSE = root / "LICENSE"
files = tuple(
    Path(path)
    for path in (
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "docs/RELEASE_EVIDENCE.md",
        "docs/DEVELOPMENT.md",
        "docs/releases/v0.1.1.md",
    )
)
try:
    repository_hygiene.validate_readme_release_state(files)
except repository_hygiene.HygieneError as error:
    print(f"[fail] {error}")
    raise SystemExit(1)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(snapshot / "scripts")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=snapshot,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.parametrize(
    "script",
    ("docs_check.py", "repository_hygiene.py"),
)
def test_final_documentation_and_hygiene_contracts(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script)],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert result.returncode == 0, result.stdout


def test_fresh_public_snapshot_contract() -> None:
    assert not (ROOT / "docs" / "STATUS.md").exists()
    assert (ROOT / "docs" / "RELEASE_EVIDENCE.md").is_file()
    assert (ROOT / "docs" / "DEVELOPMENT.md").is_file()
    assert (ROOT / "docs" / "releases" / "v0.1.1.md").is_file()
    assert (ROOT / "THIRD_PARTY_NOTICES.md").is_file()
    assert (ROOT / "LICENSE").is_file()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "(docs/RELEASE_EVIDENCE.md)" in readme
    assert "(THIRD_PARTY_NOTICES.md)" in readme
    assert "[MIT License](LICENSE)" in readme
    assert "License未選定" not in readme

    release_notes = (ROOT / "docs" / "releases" / "v0.1.1.md").read_text(
        encoding="utf-8"
    )
    assert "tag時点のapplication behaviorを記録する" in release_notes

    features = json.loads(
        (ROOT / "docs" / "features.json").read_text(encoding="utf-8")
    )["features"]
    assert len(features) == 29
    assert all(feature["passes"] is True for feature in features)
    assert all(feature["blocked"] is False for feature in features)


def test_clean_room_covers_public_runtime_acceptance() -> None:
    clean_room = (ROOT / "scripts" / "clean_room.py").read_text(encoding="utf-8")
    for command in (
        '"make", "verify"',
        '"make", "demo-smoke"',
        '"make", "e2e-kind"',
        '"make", "rollout-smoke"',
        '"make", "observability-smoke"',
        '"make", "incident-smoke"',
    ):
        assert command in clean_room


def test_repository_hygiene_accepts_post_merge_provenance(tmp_path: Path) -> None:
    snapshot = _worktree_snapshot(tmp_path)
    result = _run_repository_hygiene_release_contract(snapshot)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "fixture",
    (
        "stale_baseline",
        "stale_current_branch",
        "missing_pr_head",
        "missing_run_number",
        "missing_run_id",
        "missing_merge_commit",
        "missing_merge_boundary",
    ),
)
def test_repository_hygiene_rejects_post_merge_negative_fixtures(
    tmp_path: Path, fixture: str
) -> None:
    snapshot = _worktree_snapshot(tmp_path)
    release_evidence = snapshot / "docs" / "RELEASE_EVIDENCE.md"
    text = release_evidence.read_text(encoding="utf-8")

    if fixture == "stale_baseline":
        text += "\n公開mainの旧" + "baseline\n"
    elif fixture == "stale_current_branch":
        text += "\n現在branchはPush後のPR CIで" + "確認する\n"
    elif fixture == "missing_pr_head":
        text = text.replace("f7d2db9822215ecb8ca81e335982fb47a5c019e8", "", 1)
    elif fixture == "missing_run_number":
        text = text.replace("Run #9", "Run #", 1)
    elif fixture == "missing_run_id":
        text = text.replace("30791958394", "", 1)
    elif fixture == "missing_merge_commit":
        text = text.replace("9c342097a654c4f7f29e6c548c5870c30d7e7d8a", "", 1)
    else:
        text = text.replace(
            "merge commit固有のpush-triggered CI結果は、tracked evidence上で独立確認済みとは扱わない",
            "",
            1,
        )
    release_evidence.write_text(text, encoding="utf-8")

    result = _run_repository_hygiene_release_contract(snapshot)
    assert result.returncode != 0, result.stdout
    if fixture in {"stale_baseline", "stale_current_branch"}:
        assert "stale Hosted CI claim" in result.stdout


@pytest.mark.parametrize(
    "fixture",
    ("stale_baseline", "stale_current_branch", "mixed_ci_history", "missing_scope_adr"),
)
def test_post_merge_contract_rejects_negative_fixtures(tmp_path: Path, fixture: str) -> None:
    snapshot = _worktree_snapshot(tmp_path)
    readme = snapshot / "README.md"
    release_evidence = snapshot / "docs" / "RELEASE_EVIDENCE.md"
    goal = snapshot / "docs" / "GOAL.md"

    if fixture == "stale_baseline":
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\n公開mainの旧" + "baseline\n",
            encoding="utf-8",
        )
    elif fixture == "stale_current_branch":
        readme.write_text(
            readme.read_text(encoding="utf-8")
            + "\n現在branchはPush後のPR CIで"
            + "確認する\n",
            encoding="utf-8",
        )
    elif fixture == "mixed_ci_history":
        text = release_evidence.read_text(encoding="utf-8")
        release_evidence.write_text(
            text.replace("PR #1のPR HEADは", "PR #1のmerge commitは", 1),
            encoding="utf-8",
        )
    else:
        goal.write_text(
            goal.read_text(encoding="utf-8").replace(
                "[ADR 0005](adr/0005-aws-scope-extension.md)", "", 1
            ),
            encoding="utf-8",
        )

    result = subprocess.run(
        [sys.executable, str(snapshot / "scripts" / "docs_check.py")],
        cwd=snapshot,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert result.returncode != 0, result.stdout
