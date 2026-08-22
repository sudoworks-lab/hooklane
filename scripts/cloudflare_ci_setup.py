"""Bootstrap the credential-free Cloudflare CI environments from pinned inputs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CLOUDFLARE = ROOT / "cloudflare"
UV_ENV = CLOUDFLARE / ".venv-uv"
UV = UV_ENV / "bin" / "uv"
HARNESS_ENV = CLOUDFLARE / ".venv-harness"
HARNESS_PYTHON = HARNESS_ENV / "bin" / "python"
UV_VERSION = "0.12.3"


class SetupError(RuntimeError):
    """Raised when a pinned CI environment cannot be reproduced."""


def pinned_version(path: Path) -> str:
    version = path.read_text(encoding="utf-8").strip()
    if not version or any(character not in "0123456789." for character in version):
        raise SetupError(f"invalid version pin: {path.relative_to(ROOT)}")
    return version


def clean_environment() -> dict[str, str]:
    allowed = (
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["UV_NO_PROGRESS"] = "1"
    return environment


def run(command: list[str], *, timeout: int, cwd: Path = ROOT) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=clean_environment(),
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError(f"bootstrap command failed to execute: {command[0]}") from error
    if completed.returncode != 0:
        raise SetupError(f"bootstrap command returned {completed.returncode}: {command[0]}")


def output(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=clean_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError(f"version command failed to execute: {command[0]}") from error
    if completed.returncode != 0:
        raise SetupError(f"version command returned {completed.returncode}: {command[0]}")
    return completed.stdout.strip()


def require_version(command: list[str], expected: str, label: str) -> None:
    actual = output(command)
    if actual != expected:
        raise SetupError(f"{label} must be {expected}, found {actual or 'no version output'}")


def require_python_series(command: list[str], expected: str, label: str) -> None:
    actual = output(command)
    if not actual.startswith(f"Python {expected}."):
        raise SetupError(f"{label} must be Python {expected}.x, found {actual or 'no version output'}")


def bootstrap_uv(root_python_version: str) -> None:
    current = ".".join(str(part) for part in sys.version_info[:3])
    if current != root_python_version:
        raise SetupError(f"root bootstrap Python must be {root_python_version}, found {current}")
    if not (UV_ENV / "bin" / "python").is_file():
        run([sys.executable, "-m", "venv", str(UV_ENV)], timeout=60)
    run(
        [
            str(UV_ENV / "bin" / "python"),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--require-hashes",
            "--index-url",
            "https://pypi.org/simple",
            "--requirement",
            str(CLOUDFLARE / "uv-bootstrap.lock"),
        ],
        timeout=180,
    )
    require_version([str(UV), "--version"], f"uv {UV_VERSION} (x86_64-unknown-linux-gnu)", "uv")


def bootstrap_harness(root_python_version: str) -> None:
    run(
        [str(UV), "--no-config", "venv", "--python", sys.executable, str(HARNESS_ENV)],
        timeout=60,
    )
    run(
        [
            str(UV),
            "--no-config",
            "pip",
            "sync",
            "--python",
            str(HARNESS_PYTHON),
            "--strict",
            str(CLOUDFLARE / "harness-requirements.lock"),
        ],
        timeout=180,
    )
    require_version(
        [str(HARNESS_PYTHON), "--version"],
        f"Python {root_python_version}",
        "root mock-sink Python",
    )


def bootstrap_cloudflare(cloudflare_python_version: str) -> None:
    node_version = pinned_version(CLOUDFLARE / ".nvmrc")
    require_version(["node", "--version"], f"v{node_version}", "Node.js")
    run(
        [str(UV), "--no-config", "python", "install", cloudflare_python_version],
        timeout=180,
    )
    run(
        [
            str(UV),
            "--no-config",
            "sync",
            "--project",
            str(CLOUDFLARE),
            "--locked",
            "--python",
            cloudflare_python_version,
        ],
        timeout=300,
    )
    require_python_series(
        [str(CLOUDFLARE / ".venv" / "bin" / "python"), "--version"],
        cloudflare_python_version,
        "Cloudflare Python",
    )


def main() -> int:
    try:
        root_python_version = pinned_version(ROOT / ".python-version")
        cloudflare_python_version = pinned_version(CLOUDFLARE / ".python-version")
        bootstrap_uv(root_python_version)
        bootstrap_harness(root_python_version)
        bootstrap_cloudflare(cloudflare_python_version)
    except SetupError as error:
        print(f"[fail] Cloudflare CI setup: {error}")
        return 1
    print(
        "[ok] Cloudflare CI setup: pinned uv, isolated Python 3.12 harness, "
        "and isolated Python 3.13 Worker environment"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
