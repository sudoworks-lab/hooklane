"""Operate only Hooklane's project-specific local kind cluster."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Never


ROOT = Path(__file__).resolve().parents[1]
CLUSTER_NAME = "hooklane-f014"
CONTEXT_NAME = f"kind-{CLUSTER_NAME}"
KUBECONFIG = Path("/tmp/hooklane-f014-kubeconfig")
NAMESPACE = "hooklane"
RELEASE = "hooklane"
KIND_CONFIG = ROOT / "deploy" / "kind" / "cluster.yaml"
CHART = ROOT / "charts" / "hooklane"
DEFAULT_IMAGE_TAG = "0.1.1"
IMAGE_TAG_PATTERN = re.compile(r"(?:0\.1\.1|git-[0-9a-f]{40})\Z")


def fail(message: str) -> Never:
    raise RuntimeError(message)


def resolve_image_tag(value: str | None = None) -> str:
    candidate = os.environ.get("IMAGE_TAG", DEFAULT_IMAGE_TAG) if value is None else value
    if not IMAGE_TAG_PATTERN.fullmatch(candidate):
        raise ValueError(
            "IMAGE_TAG must be exactly 0.1.1 or git-<40 lowercase hexadecimal characters>"
        )
    return candidate


def application_images(image_tag: str) -> tuple[str, ...]:
    tag = resolve_image_tag(image_tag)
    return tuple(f"{name}:{tag}" for name in ("hooklane-api", "hooklane-worker", "hooklane-mock-sink"))


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, check=check, text=True)


def output(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def clusters() -> set[str]:
    return {line.strip() for line in output(["kind", "get", "clusters"]).splitlines() if line.strip()}


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "kubectl",
            "--kubeconfig",
            str(KUBECONFIG),
            "--context",
            CONTEXT_NAME,
            *args,
        ],
        check=check,
    )


def helm(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "helm",
            "--kubeconfig",
            str(KUBECONFIG),
            "--kube-context",
            CONTEXT_NAME,
            *args,
        ],
        check=check,
    )


def require_cluster() -> None:
    if CLUSTER_NAME not in clusters():
        fail(f"project cluster {CLUSTER_NAME} does not exist")
    if not KUBECONFIG.is_file():
        run(
            [
                "kind",
                "export",
                "kubeconfig",
                "--name",
                CLUSTER_NAME,
                "--kubeconfig",
                str(KUBECONFIG),
            ]
        )


def cluster_up() -> None:
    if CLUSTER_NAME in clusters():
        run(
            [
                "kind",
                "export",
                "kubeconfig",
                "--name",
                CLUSTER_NAME,
                "--kubeconfig",
                str(KUBECONFIG),
            ]
        )
        print(f"[ok] reusing project cluster {CLUSTER_NAME}")
    else:
        run(
            [
                "kind",
                "create",
                "cluster",
                "--name",
                CLUSTER_NAME,
                "--config",
                str(KIND_CONFIG),
                "--kubeconfig",
                str(KUBECONFIG),
                "--wait",
                "180s",
            ]
        )
    kubectl("wait", "--for=condition=Ready", "node", "--all", "--timeout=180s")
    print(f"[ok] project cluster {CLUSTER_NAME} is Ready")


def load_images(image_tag: str | None = None) -> None:
    resolved_tag = resolve_image_tag(image_tag)
    require_cluster()
    for image in application_images(resolved_tag):
        run(["kind", "load", "docker-image", image, "--name", CLUSTER_NAME])
    print("[ok] commit-tagged Hooklane images loaded into kind")


def image_overrides(image_tag: str) -> tuple[str, ...]:
    return tuple(
        value
        for name in ("api", "worker", "mockSink")
        for value in ("--set-string", f"{name}.image.tag={image_tag}")
    )


def deploy() -> None:
    require_cluster()
    image_tag = resolve_image_tag()
    load_images(image_tag)
    helm(
        "upgrade",
        "--install",
        RELEASE,
        str(CHART),
        "--namespace",
        NAMESPACE,
        "--create-namespace",
        "--wait",
        "--timeout",
        "180s",
        "--history-max",
        "3",
        *image_overrides(image_tag),
    )
    kubectl("--namespace", NAMESPACE, "rollout", "status", "deployment/hooklane-api", "--timeout=180s")
    kubectl("--namespace", NAMESPACE, "rollout", "status", "deployment/hooklane-worker", "--timeout=180s")
    kubectl(
        "--namespace",
        NAMESPACE,
        "rollout",
        "status",
        "deployment/hooklane-mock-sink",
        "--timeout=180s",
    )
    kubectl("--namespace", NAMESPACE, "rollout", "status", "statefulset/hooklane-redis", "--timeout=180s")
    print("[ok] Helm release and four workloads are Ready")


def helm_test() -> None:
    require_cluster()
    helm("test", RELEASE, "--namespace", NAMESPACE, "--logs", "--timeout", "60s")
    print("[ok] Helm test passed")


def diagnostics() -> None:
    require_cluster()
    commands = (
        ("get", "pods", "--namespace", NAMESPACE, "-o", "wide"),
        ("get", "events", "--namespace", NAMESPACE, "--sort-by=.lastTimestamp"),
        ("describe", "deployments", "--namespace", NAMESPACE),
        ("describe", "statefulsets", "--namespace", NAMESPACE),
        (
            "logs",
            "--namespace",
            NAMESPACE,
            "--selector",
            f"app.kubernetes.io/instance={RELEASE}",
            "--all-containers=true",
            "--tail=100",
            "--prefix=true",
        ),
    )
    failures = sum(kubectl(*command, check=False).returncode != 0 for command in commands)
    if failures:
        fail(f"{failures} diagnostic commands failed")
    print("[ok] pods, events, describe, and logs diagnostics are available")


def cluster_down() -> None:
    if CLUSTER_NAME in clusters():
        run(
            [
                "kind",
                "delete",
                "cluster",
                "--name",
                CLUSTER_NAME,
                "--kubeconfig",
                str(KUBECONFIG),
            ]
        )
    KUBECONFIG.unlink(missing_ok=True)
    if CLUSTER_NAME in clusters():
        fail(f"project cluster {CLUSTER_NAME} still exists")
    print(f"[ok] project cluster {CLUSTER_NAME} removed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("cluster-up", "deploy", "helm-test", "diagnostics", "cluster-down"),
    )
    return parser.parse_args()


def main() -> int:
    command = parse_args().command
    if command == "cluster-up":
        cluster_up()
    elif command == "deploy":
        deploy()
    elif command == "helm-test":
        helm_test()
    elif command == "diagnostics":
        diagnostics()
    else:
        cluster_down()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[fail] kind runtime: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
