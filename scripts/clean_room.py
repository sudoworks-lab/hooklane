"""Verify Hooklane from a local, tracked, disposable Git candidate."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tempfile
from typing import Never


ROOT = Path(__file__).resolve().parents[1]
CLUSTER_NAME = "hooklane-f014"
KUBECONFIG = Path("/tmp/hooklane-f014-kubeconfig")
COMPOSE_PROJECT = "hooklane-f012"


class CleanRoomError(RuntimeError):
    """Raised when isolation, verification, or cleanup fails."""


def fail(message: str) -> Never:
    raise CleanRoomError(message)


def run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = False,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def output(command: list[str], *, cwd: Path) -> str:
    result = run(command, cwd=cwd, capture=True, timeout=60)
    return result.stdout


def nonempty_lines(value: str) -> tuple[str, ...]:
    return tuple(line for line in value.splitlines() if line)


def kind_clusters(*, cwd: Path) -> tuple[str, ...]:
    return nonempty_lines(output(["kind", "get", "clusters"], cwd=cwd))


def docker_ids(*args: str, cwd: Path) -> tuple[str, ...]:
    result = run(["docker", *args], cwd=cwd, capture=True, check=False, timeout=30)
    if result.returncode != 0:
        fail("Docker runtime hygiene query failed")
    return nonempty_lines(result.stdout)


def runtime_findings(*, repository: Path) -> tuple[str, ...]:
    findings: list[str] = []
    if CLUSTER_NAME in kind_clusters(cwd=repository):
        findings.append("project kind cluster")
    if KUBECONFIG.exists():
        findings.append("dedicated kubeconfig")

    label = f"label=com.docker.compose.project={COMPOSE_PROJECT}"
    if docker_ids("ps", "--all", "--filter", label, "--quiet", cwd=repository):
        findings.append("Hooklane Compose container")
    if docker_ids("network", "ls", "--filter", label, "--quiet", cwd=repository):
        findings.append("Hooklane Compose network")
    if docker_ids("volume", "ls", "--filter", label, "--quiet", cwd=repository):
        findings.append("Hooklane Compose volume")
    if docker_ids(
        "ps",
        "--all",
        "--filter",
        "name=hooklane-integration-",
        "--quiet",
        cwd=repository,
    ):
        findings.append("integration Redis container")

    artifacts = repository / "artifacts"
    if artifacts.is_dir() and any(
        path.is_file() for directory in artifacts.glob("kind-e2e-*") for path in directory.rglob("*")
    ):
        findings.append("diagnostics artifact")
    return tuple(findings)


def require_runtime_clean(*, repository: Path) -> None:
    findings = runtime_findings(repository=repository)
    if findings:
        fail(f"runtime hygiene found: {', '.join(findings)}")
    print("[ok] no Hooklane cluster, container, network, volume, kubeconfig, test Redis, or artifact")


def cleanup_runtime(*, repository: Path) -> bool:
    passed = True
    try:
        if CLUSTER_NAME in kind_clusters(cwd=repository):
            result = run(
                ["make", "cluster-down"],
                cwd=repository,
                check=False,
                timeout=300,
            )
            passed = result.returncode == 0 and passed
    except (OSError, CleanRoomError, subprocess.TimeoutExpired):
        passed = False

    label = f"label=com.docker.compose.project={COMPOSE_PROJECT}"
    try:
        compose_exists = any(
            (
                docker_ids("ps", "--all", "--filter", label, "--quiet", cwd=repository),
                docker_ids("network", "ls", "--filter", label, "--quiet", cwd=repository),
                docker_ids("volume", "ls", "--filter", label, "--quiet", cwd=repository),
            )
        )
        if compose_exists:
            result = run(
                ["make", "compose-down"],
                cwd=repository,
                check=False,
                timeout=180,
            )
            passed = result.returncode == 0 and passed
    except (OSError, CleanRoomError, subprocess.TimeoutExpired):
        passed = False
    return passed


def source_candidate_state() -> tuple[tuple[str, ...], str]:
    unstaged = nonempty_lines(output(["git", "diff", "--name-only"], cwd=ROOT))
    untracked = nonempty_lines(
        output(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT)
    )
    if unstaged or untracked:
        fail("clean-room candidate must contain only explicitly staged changes")
    staged = nonempty_lines(output(["git", "diff", "--cached", "--name-only"], cwd=ROOT))
    tree = output(["git", "write-tree"], cwd=ROOT).strip()
    if not tree:
        fail("source candidate tree is unavailable")
    return staged, tree


def create_candidate_clone(*, parent: Path) -> Path:
    staged, expected_tree = source_candidate_state()
    clone = parent / "repository"
    run(
        ["git", "clone", "--local", "--no-hardlinks", "--quiet", str(ROOT), str(clone)],
        cwd=parent,
        timeout=300,
    )

    if staged:
        patch = parent / "candidate.patch"
        run(
            [
                "git",
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                f"--output={patch}",
            ],
            cwd=ROOT,
            timeout=120,
        )
        run(["git", "apply", "--index", str(patch)], cwd=clone, timeout=120)
        run(
            [
                "git",
                "-c",
                "user.name=clean-room",
                "-c",
                "user.email=clean-room.invalid",
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "--no-verify",
                "-m",
                "clean-room candidate",
            ],
            cwd=clone,
            timeout=120,
        )

    candidate_tree = output(["git", "rev-parse", "HEAD^{tree}"], cwd=clone).strip()
    if candidate_tree != expected_tree:
        fail("temporary clone tree does not match the source index")
    if output(["git", "status", "--porcelain"], cwd=clone):
        fail("temporary clone is not clean")
    if (clone / ".venv").exists() or (clone / "artifacts").exists():
        fail("temporary clone copied a source cache or artifact")
    print(
        "[ok] local no-hardlink clone matches the tracked HEAD/index tree; "
        "untracked files and caches were not copied"
    )
    return clone


def run_stage(repository: Path, label: str, command: list[str], *, timeout: int) -> None:
    print(f"[clean-room:start] {label}", flush=True)
    run(command, cwd=repository, timeout=timeout)
    print(f"[clean-room:pass] {label}", flush=True)


def verify_candidate(repository: Path) -> None:
    stages: tuple[tuple[str, list[str], int], ...] = (
        ("initialization", ["bash", "scripts/init.sh"], 300),
        ("pinned dependency setup", ["make", "ci-setup"], 900),
        ("quality-security-chart-docs", ["make", "verify"], 1200),
        ("Compose demo", ["make", "demo-smoke"], 900),
        ("kind E2E", ["make", "e2e-kind"], 1200),
        ("kind cluster", ["make", "cluster-up"], 600),
        ("Helm deploy", ["make", "deploy"], 900),
        ("Helm demo", ["make", "chart-smoke"], 600),
        ("rolling update and rollback", ["make", "rollout-smoke"], 1200),
        ("observability deploy", ["make", "observability-up"], 900),
        ("observability smoke", ["make", "observability-smoke"], 1200),
        ("incident aggregate", ["make", "incident-smoke"], 1800),
        ("documentation", ["make", "docs-check"], 300),
        ("diff hygiene", ["git", "diff", "--check"], 60),
    )
    for label, command, timeout in stages:
        run_stage(repository, label, command, timeout=timeout)


def run_clean_room() -> None:
    require_runtime_clean(repository=ROOT)
    passed = True
    with tempfile.TemporaryDirectory(prefix="hooklane-clean-room-") as raw_directory:
        parent = Path(raw_directory)
        clone: Path | None = None
        try:
            clone = create_candidate_clone(parent=parent)
            verify_candidate(clone)
        except (
            CleanRoomError,
            OSError,
            RuntimeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            passed = False
            print(f"[fail] clean-room verification: {type(error).__name__}")
        finally:
            cleanup_root = clone if clone is not None else ROOT
            if not cleanup_runtime(repository=cleanup_root):
                passed = False
                print("[fail] clean-room project runtime cleanup failed")
            try:
                require_runtime_clean(repository=cleanup_root)
            except (OSError, CleanRoomError):
                passed = False
                print("[fail] clean-room runtime hygiene failed after cleanup")
    require_runtime_clean(repository=ROOT)
    if not passed:
        fail("clean-room candidate did not pass every stage")
    print("[ok] clean-room full verification passed and temporary clone was removed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if bool(args.runtime_only):
            require_runtime_clean(repository=ROOT)
        else:
            run_clean_room()
    except (OSError, CleanRoomError, subprocess.CalledProcessError) as error:
        detail = str(error) if isinstance(error, CleanRoomError) else type(error).__name__
        print(f"[fail] clean-room: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
