"""Check Hooklane's local prerequisites without printing environment values."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ToolCheck:
    label: str
    binary: str
    args: tuple[str, ...]


TOOL_CHECKS = {
    "bash": ToolCheck("Bash", "bash", ("--version",)),
    "git": ToolCheck("Git", "git", ("--version",)),
    "make": ToolCheck("Make", "make", ("--version",)),
    "docker": ToolCheck("Docker", "docker", ("--version",)),
    "docker_compose": ToolCheck("Docker Compose", "docker", ("compose", "version")),
    "kubectl": ToolCheck("kubectl", "kubectl", ("version", "--client")),
    "kind": ToolCheck("kind", "kind", ("version",)),
    "helm": ToolCheck("Helm", "helm", ("version", "--short")),
    "gitleaks": ToolCheck("Gitleaks", "gitleaks", ("version",)),
    "osv_scanner": ToolCheck("OSV-Scanner", "osv-scanner", ("--version",)),
    "trivy": ToolCheck("Trivy", "trivy", ("--version",)),
    "kubeconform": ToolCheck("Kubeconform", "kubeconform", ("-v",)),
}


class PythonConfig(TypedDict):
    version: str


class ToolGroups(TypedDict):
    required: dict[str, str]
    optional: dict[str, str]


class ResourceConfig(TypedDict):
    min_cpus: int
    min_memory_gib: int
    min_disk_gib: int


class ToolchainConfig(TypedDict):
    python: PythonConfig
    tools: ToolGroups
    resources: ResourceConfig


def load_toolchain() -> ToolchainConfig:
    with (ROOT / "toolchain.toml").open("rb") as handle:
        return cast(ToolchainConfig, tomllib.load(handle))


def has_exact_version(output: str, expected: str) -> bool:
    pattern = rf"(?<![0-9.])v?{re.escape(expected)}(?![0-9.])"
    return re.search(pattern, output) is not None


def run_tool(check: ToolCheck) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (check.binary, *check.args),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def check_tool(name: str, expected: str, *, optional: bool) -> bool:
    check = TOOL_CHECKS[name]
    if shutil.which(check.binary) is None:
        status = "skip" if optional else "fail"
        print(f"[{status}] {check.label}: command not found (pinned {expected})")
        return optional

    result = run_tool(check)
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        print(f"[fail] {check.label}: version command exited {result.returncode}")
        return False
    if not has_exact_version(output, expected):
        print(f"[fail] {check.label}: installed version does not match pinned {expected}")
        return False

    print(f"[ok] {check.label}: pinned version {expected}")
    return True


def check_docker_daemon(expected: str) -> bool:
    result = subprocess.run(
        ("docker", "info", "--format", "{{.ServerVersion}}"),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        error = result.stderr.lower()
        socket_path = Path("/var/run/docker.sock")
        socket_has_access = (
            socket_path.exists()
            and stat.S_ISSOCK(socket_path.stat().st_mode)
            and os.access(socket_path, os.R_OK | os.W_OK)
        )
        policy_denied = "operation not permitted" in error or "permission denied" in error
        if policy_denied and socket_has_access:
            print("[skip] Docker daemon: host policy denied nested socket access")
            return True
        print("[fail] Docker daemon: connection failed")
        return False
    if result.stdout.strip() != expected:
        print(f"[fail] Docker daemon: server version does not match pinned {expected}")
        return False
    print("[ok] Docker daemon: reachable with pinned server version")
    return True


def memory_gib() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024 // 1024
    return 0


def check_resources(config: ResourceConfig) -> bool:
    checks = {
        "CPU": (os.cpu_count() or 0, config["min_cpus"], "cores"),
        "memory": (memory_gib(), config["min_memory_gib"], "GiB available"),
        "disk": (
            shutil.disk_usage(ROOT).free // 1024**3,
            config["min_disk_gib"],
            "GiB available",
        ),
    }
    passed = True
    for label, (actual, minimum, unit) in checks.items():
        if actual < minimum:
            print(f"[fail] {label}: {actual} {unit}; minimum is {minimum}")
            passed = False
        else:
            print(f"[ok] {label}: {actual} {unit}; minimum is {minimum}")
    return passed


def main() -> int:
    config = load_toolchain()
    passed = True

    python_version = str(config["python"]["version"])
    current_python = ".".join(str(part) for part in sys.version_info[:3])
    if current_python == python_version:
        print(f"[ok] Python: pinned version {python_version}")
    else:
        print(f"[fail] Python: installed {current_python}; pinned {python_version}")
        passed = False

    for name, expected in config["tools"]["required"].items():
        passed = check_tool(name, str(expected), optional=False) and passed
    for name, expected in config["tools"]["optional"].items():
        passed = check_tool(name, str(expected), optional=True) and passed

    passed = check_docker_daemon(str(config["tools"]["required"]["docker"])) and passed
    passed = check_resources(config["resources"]) and passed
    print("[info] Environment variable values were not inspected or printed.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
