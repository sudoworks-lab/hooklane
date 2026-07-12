"""Verify built application images without reading runtime configuration values."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Never, TypedDict, cast


ROOT = Path(__file__).resolve().parents[1]
BASE_DIGEST = (
    "python:3.12-alpine3.23@sha256:"
    "601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d"
)


class ImageExpectation(TypedDict):
    name: str
    entrypoint: list[str]
    module: str


IMAGES: dict[str, ImageExpectation] = {
    "api": {
        "name": "hooklane-api:0.1.0",
        "entrypoint": ["uvicorn"],
        "module": "hooklane.api.app",
    },
    "worker": {
        "name": "hooklane-worker:0.1.0",
        "entrypoint": ["python", "-m", "hooklane.worker.main"],
        "module": "hooklane.worker.main",
    },
    "mock-sink": {
        "name": "hooklane-mock-sink:0.1.0",
        "entrypoint": ["uvicorn"],
        "module": "hooklane.mock_sink.app",
    },
}
OBSERVABILITY_IMAGES = {
    "prometheus": {
        "reference": (
            "prom/prometheus@sha256:"
            "f39df5334dee301b885f77e0ff1159f5d8a43bf9db518f885544594799a1e3c2"
        ),
        "version": "v3.12.0-distroless",
        "digest": "f39df5334dee301b885f77e0ff1159f5d8a43bf9db518f885544594799a1e3c2",
        "user": "65532",
    },
    "grafana": {
        "reference": (
            "grafana/grafana@sha256:"
            "5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2"
        ),
        "version": "13.0.2",
        "digest": "5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2",
        "user": "472",
    },
}
FORBIDDEN_PACKAGES = ("pytest", "mypy", "ruff")
FORBIDDEN_PATHS = ("/app/.git", "/app/tests", "/app/logs", "/app/.env")


def fail(message: str) -> Never:
    raise RuntimeError(message)


def inspect_image(name: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "image", "inspect", name],
        check=True,
        capture_output=True,
        text=True,
    )
    result: object = json.loads(completed.stdout)
    if not isinstance(result, list) or len(result) != 1:
        fail(f"unexpected inspect result for {name}")
    image = result[0]
    if not isinstance(image, dict):
        fail(f"invalid inspect result for {name}")
    return cast(dict[str, Any], image)


def verify_files() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    if BASE_DIGEST not in dockerfile:
        fail("Dockerfile base image is not pinned to the approved digest")
    if ":latest" in dockerfile:
        fail("Dockerfile must not use latest")

    ignore_entries = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    required = {".git", ".venv", "tests", "logs", ".env", ".env.*", "__pycache__"}
    missing = sorted(required - ignore_entries)
    if missing:
        fail(f".dockerignore is missing required entries: {', '.join(missing)}")


def verify_runtime_contents(image_name: str, module: str) -> None:
    script = """
import importlib.util
from pathlib import Path
import sys

module = sys.argv[1]
forbidden_packages = sys.argv[2].split(',')
forbidden_paths = sys.argv[3].split(',')
if importlib.util.find_spec(module) is None:
    raise SystemExit(f'missing runtime module: {module}')
present_packages = [name for name in forbidden_packages if importlib.util.find_spec(name)]
if present_packages:
    raise SystemExit('development packages present: ' + ','.join(present_packages))
present_paths = [name for name in forbidden_paths if Path(name).exists()]
if present_paths:
    raise SystemExit('forbidden paths present: ' + ','.join(present_paths))
"""
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--entrypoint",
            "python",
            image_name,
            "-c",
            script,
            module,
            ",".join(FORBIDDEN_PACKAGES),
            ",".join(FORBIDDEN_PATHS),
        ],
        check=True,
    )


def verify_process_starts(role: str, image_name: str) -> None:
    container_name = f"hooklane-f011-{role}-{os.getpid()}"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--detach",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            image_name,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        for _attempt in range(20):
            completed = subprocess.run(
                [
                    "docker",
                    "container",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    container_name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            if completed.stdout.strip() == "true":
                time.sleep(0.2)
                confirmation = subprocess.run(
                    [
                        "docker",
                        "container",
                        "inspect",
                        "--format",
                        "{{.State.Running}}",
                        container_name,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if confirmation.stdout.strip() == "true":
                    return
            time.sleep(0.05)
        fail(f"{image_name} did not keep its configured process running")
    finally:
        subprocess.run(
            ["docker", "stop", "--time", "2", container_name],
            check=False,
            capture_output=True,
            text=True,
        )


def verify_observability_images() -> None:
    values = (ROOT / "charts" / "hooklane" / "values.yaml").read_text(encoding="utf-8")
    policy_object: object = json.loads(
        (ROOT / "container-policy.json").read_text(encoding="utf-8")
    )
    if not isinstance(policy_object, dict):
        fail("container policy root must be an object")
    services = cast(dict[str, Any], policy_object.get("services"))
    for service, expected in OBSERVABILITY_IMAGES.items():
        reference = expected["reference"]
        digest = expected["digest"]
        version = expected["version"]
        image = inspect_image(reference)
        config = cast(dict[str, Any], image.get("Config"))
        if config.get("User") != expected["user"]:
            fail(f"{service} image does not declare the approved non-root user")
        repo_digests = image.get("RepoDigests")
        if not isinstance(repo_digests, list) or not any(
            isinstance(value, str) and value.endswith(f"@sha256:{digest}")
            for value in repo_digests
        ):
            fail(f"{service} local image does not match its approved registry digest")
        if f"tag: {version}" not in values or f"digest: sha256:{digest}" not in values:
            fail(f"{service} chart tag and digest are not fixed")
        policy_entry = services.get(service)
        if not isinstance(policy_entry, dict) or policy_entry.get("image") != (
            f"{reference.split('@', maxsplit=1)[0]}:{version}@sha256:{digest}"
        ):
            fail(f"{service} policy image does not match chart pin")
        print(f"[ok] {service}: exact version, digest, and non-root image user passed")


def main() -> int:
    verify_files()
    for role, expected in IMAGES.items():
        name = expected["name"]
        if not isinstance(name, str) or name.endswith(":latest"):
            fail(f"{role} image tag is not fixed")
        image = inspect_image(name)
        config_object = image.get("Config")
        if not isinstance(config_object, dict):
            fail(f"{name} has no image configuration")
        config = cast(dict[str, Any], config_object)
        if config.get("User") != "10001:10001":
            fail(f"{name} does not run as the fixed non-root user")
        if config.get("Entrypoint") != expected["entrypoint"]:
            fail(f"{name} has an unexpected entrypoint")
        labels_object = config.get("Labels") or {}
        if not isinstance(labels_object, dict):
            fail(f"{name} labels are invalid")
        labels = cast(dict[str, Any], labels_object)
        if labels.get("org.opencontainers.image.version") != "0.1.0":
            fail(f"{name} has no fixed version label")
        if labels.get("io.hooklane.role") != role:
            fail(f"{name} role label does not match")
        verify_runtime_contents(name, expected["module"])
        verify_process_starts(role, name)
        print(f"[ok] {name}: pinned, non-root, minimal runtime contract passed")
    verify_observability_images()
    print("[ok] Dockerfile and .dockerignore contracts passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"[fail] image contract: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
