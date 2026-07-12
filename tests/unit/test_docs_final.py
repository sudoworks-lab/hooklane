"""Unit coverage for final public documentation and repository hygiene."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


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
    assert (ROOT / "THIRD_PARTY_NOTICES.md").is_file()
    assert (ROOT / "LICENSE").is_file()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "(docs/RELEASE_EVIDENCE.md)" in readme
    assert "(THIRD_PARTY_NOTICES.md)" in readme
    assert "[MIT License](LICENSE)" in readme
    assert "License未選定" not in readme

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
