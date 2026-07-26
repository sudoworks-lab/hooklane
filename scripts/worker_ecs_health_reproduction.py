"""Reproduce the worker ECS liveness contract with disposable local containers."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Final, Never


IMAGE: Final = "hooklane-worker:0.1.1"
REDIS_IMAGE: Final = (
    "redis:8.0.1-alpine@sha256:"
    "62b5498c91778f738f0efbf0a6fd5b434011235a3e7b5f2ed4a2c0c63bb1c786"
)
METRICS_PORT: Final = 9090
ECS_START_PERIOD_SECONDS: Final = 30
WORKER_LIVENESS_COMMAND: Final = (
    'python -c "import urllib.request; '
    "urllib.request.urlopen('http://127.0.0.1:9090/metrics', timeout=2).close()\""
)


def fail(message: str) -> Never:
    raise RuntimeError(message)


def docker(arguments: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def require(arguments: list[str], operation: str, *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = docker(arguments, timeout=timeout)
    if result.returncode != 0:
        fail(f"{operation} failed")
    return result


def is_running(container_name: str) -> bool:
    result = require(
        ["container", "inspect", "--format", "{{.State.Running}}", container_name],
        "inspect worker container",
    )
    return result.stdout.strip() == "true"


def wait_for_exit(container_name: str) -> int:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not is_running(container_name):
            result = require(
                ["container", "inspect", "--format", "{{.State.ExitCode}}", container_name],
                "inspect worker exit code",
            )
            return int(result.stdout.strip())
        time.sleep(0.2)
    fail("worker did not stop after SIGTERM")


def worker_events(container_name: str) -> list[str]:
    result = require(["logs", container_name], "collect worker logs")
    events: list[str] = []
    for line in (*result.stdout.splitlines(), *result.stderr.splitlines()):
        parsed: object
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        event = parsed.get("event")
        if event in {"worker_started", "worker_stopped"}:
            events.append(event)
    return events


def wait_for_command(
    container_name: str,
    command: str,
    *,
    expected_exit: int,
) -> tuple[int, int]:
    started = time.monotonic()
    last_exit = 255
    while time.monotonic() - started < ECS_START_PERIOD_SECONDS:
        result = docker(["exec", container_name, "/bin/sh", "-c", command], timeout=8)
        last_exit = result.returncode
        if last_exit == expected_exit:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            return last_exit, elapsed_ms
        time.sleep(0.25)
    return last_exit, round((time.monotonic() - started) * 1000)


def command_exit(container_name: str, command: str) -> int:
    return docker(["exec", container_name, "/bin/sh", "-c", command], timeout=8).returncode


def wait_for_redis(container_name: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if docker(["exec", container_name, "redis-cli", "ping"], timeout=5).returncode == 0:
            return
        time.sleep(0.2)
    fail("disposable Redis did not become ready")


def start_worker(
    *,
    container_name: str,
    network_name: str,
    redis_connection: str,
) -> None:
    require(
        [
            "run",
            "--detach",
            "--name",
            container_name,
            "--network",
            network_name,
            "--user",
            "10001:10001",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m,uid=10001,gid=10001,mode=0700",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--env",
            f"HOOKLANE_REDIS_URL={redis_connection}",
            "--env",
            f"HOOKLANE_CONSUMER_NAME={container_name}",
            IMAGE,
        ],
        "start worker container",
    )


def stop_and_collect(container_name: str) -> tuple[int, list[str]]:
    require(["kill", "--signal", "TERM", container_name], "send SIGTERM to worker")
    return wait_for_exit(container_name), worker_events(container_name)


def cleanup(containers: list[str], network_name: str, network_created: bool) -> None:
    for container_name in reversed(containers):
        docker(["rm", "--force", container_name], timeout=20)
    if network_created:
        docker(["network", "rm", network_name], timeout=20)


def main() -> int:
    nonce = f"hooklane-worker-health-{os.getpid()}"
    network_name = f"{nonce}-network"
    redis_name = f"{nonce}-redis"
    healthy_worker_name = f"{nonce}-healthy"
    unavailable_worker_name = f"{nonce}-unavailable"
    containers: list[str] = []
    network_created = False

    try:
        require(
            ["run", "--rm", "--network", "none", "--entrypoint", "python", IMAGE, "--version"],
            "verify Python in worker image",
        )
        require(["network", "create", network_name], "create disposable Docker network")
        network_created = True
        require(
            [
                "run",
                "--detach",
                "--name",
                redis_name,
                "--network",
                network_name,
                "--network-alias",
                "redis",
                "--user",
                "999:1000",
                "--read-only",
                "--tmpfs",
                "/data:rw,noexec,nosuid,size=64m,uid=999,gid=1000,mode=0700",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m,uid=999,gid=1000,mode=0700",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                REDIS_IMAGE,
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                "/data",
            ],
            "start disposable Redis",
        )
        containers.append(redis_name)
        wait_for_redis(redis_name)

        start_worker(
            container_name=healthy_worker_name,
            network_name=network_name,
            redis_connection="redis://redis:6379/0",
        )
        containers.append(healthy_worker_name)
        healthy_probe_exit, healthy_ready_ms = wait_for_command(
            healthy_worker_name,
            WORKER_LIVENESS_COMMAND,
            expected_exit=0,
        )
        healthy_original_exit = command_exit(
            healthy_worker_name,
            "python -m hooklane.worker.health startup",
        )
        healthy_running = is_running(healthy_worker_name)
        healthy_exit, healthy_events = stop_and_collect(healthy_worker_name)

        start_worker(
            container_name=unavailable_worker_name,
            network_name=network_name,
            redis_connection="redis://redis:6390/0",
        )
        containers.append(unavailable_worker_name)
        unavailable_probe_exit, unavailable_ready_ms = wait_for_command(
            unavailable_worker_name,
            WORKER_LIVENESS_COMMAND,
            expected_exit=0,
        )
        original_dependency_exits = [
            command_exit(unavailable_worker_name, "python -m hooklane.worker.health startup")
            for _ in range(3)
        ]
        unavailable_liveness_exit = command_exit(unavailable_worker_name, WORKER_LIVENESS_COMMAND)
        unavailable_running = is_running(unavailable_worker_name)
        unavailable_exit, unavailable_events = stop_and_collect(unavailable_worker_name)

        no_listener_exit = docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m,uid=10001,gid=10001,mode=0700",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--entrypoint",
                "/bin/sh",
                IMAGE,
                "-c",
                WORKER_LIVENESS_COMMAND,
            ],
            timeout=15,
        ).returncode

        if healthy_probe_exit != 0 or healthy_original_exit != 0 or not healthy_running:
            fail("worker did not become healthy with Redis available")
        if healthy_ready_ms >= ECS_START_PERIOD_SECONDS * 1000:
            fail("worker metrics did not start within the ECS start period")
        if healthy_exit != 0 or healthy_events != ["worker_started", "worker_stopped"]:
            fail("worker graceful SIGTERM contract failed with Redis available")
        if unavailable_probe_exit != 0 or unavailable_liveness_exit != 0 or not unavailable_running:
            fail("local liveness did not survive unavailable Redis")
        if unavailable_ready_ms >= ECS_START_PERIOD_SECONDS * 1000:
            fail("worker metrics did not start within the ECS start period without Redis")
        if any(exit_code == 0 for exit_code in original_dependency_exits):
            fail("original dependency health command unexpectedly passed without Redis")
        if unavailable_exit != 0 or unavailable_events != ["worker_started", "worker_stopped"]:
            fail("worker graceful SIGTERM contract failed without Redis")
        if no_listener_exit == 0:
            fail("local liveness command passed without a metrics listener")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print("[fail] worker ECS health reproduction failed without emitting runtime configuration")
        return 1
    finally:
        cleanup(containers, network_name, network_created)

    print(
        "[ok] worker ECS liveness used Python on port "
        f"{METRICS_PORT}; available and unavailable Redis startup timings were "
        f"{healthy_ready_ms}ms and {unavailable_ready_ms}ms; disposable resources were removed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
