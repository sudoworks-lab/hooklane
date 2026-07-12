"""Unit coverage for the core documentation contract."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_core_documentation_matches_repository_contracts() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "docs_core_check.py")],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert result.returncode == 0, result.stdout
