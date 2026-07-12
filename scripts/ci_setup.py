"""Prepare the pinned Python environment and CI tools without reading credentials."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"


def run(command: list[str], *, timeout: int) -> bool:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def main() -> int:
    current_python = ".".join(str(part) for part in sys.version_info[:3])
    if current_python != "3.12.3":
        print(f"[fail] CI Python must be 3.12.3, found {current_python}")
        return 1

    if not VENV_PYTHON.is_file() and not run(
        [sys.executable, "-m", "venv", str(VENV)],
        timeout=60,
    ):
        print("[fail] CI virtual environment creation failed")
        return 1

    if not run(
        [
            str(VENV_PYTHON),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--index-url",
            "https://pypi.org/simple",
            "--requirement",
            "requirements.lock",
        ],
        timeout=300,
    ):
        print("[fail] pinned Python dependency installation failed")
        return 1
    if not run([str(VENV_PYTHON), "-m", "pip", "check"], timeout=30):
        print("[fail] pinned Python dependency consistency check failed")
        return 1
    print("[ok] pinned Python dependencies are installed and consistent")

    tools = subprocess.run(
        [str(VENV_PYTHON), "scripts/install_ci_tools.py"],
        cwd=ROOT,
        check=False,
        text=True,
        timeout=600,
    )
    if tools.returncode != 0:
        print("[fail] pinned CI tool installation failed")
        return 1
    print("[ok] CI setup completed without reading environment or credential values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
