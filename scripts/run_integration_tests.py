"""Run integration tests against an isolated, disposable Redis container."""

from __future__ import annotations

import os
import subprocess
import sys
import time


REDIS_IMAGE = (
    "redis:8.0.1-alpine@"
    "sha256:62b5498c91778f738f0efbf0a6fd5b434011235a3e7b5f2ed4a2c0c63bb1c786"
)


def docker(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def wait_for_redis(container_name: str) -> bool:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = docker(["exec", container_name, "redis-cli", "ping"])
        if result.returncode == 0 and result.stdout.strip() == "PONG":
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    tests = sys.argv[1:] or ["tests/integration"]
    container_name = f"hooklane-integration-{os.getpid()}"
    created = False
    try:
        started = docker(
            [
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--publish",
                "127.0.0.1::6379",
                "--tmpfs",
                "/data",
                REDIS_IMAGE,
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "no",
            ]
        )
        if started.returncode != 0:
            print(f"[fail] integration Redis start exited {started.returncode}")
            return 1
        created = True

        port_result = docker(["port", container_name, "6379/tcp"])
        if port_result.returncode != 0 or ":" not in port_result.stdout:
            print("[fail] integration Redis port discovery failed")
            return 1
        port = port_result.stdout.strip().rsplit(":", maxsplit=1)[1]
        if not port.isdigit() or not wait_for_redis(container_name):
            print("[fail] integration Redis did not become ready")
            return 1

        os.environ["HOOKLANE_TEST_REDIS_URL"] = f"redis://127.0.0.1:{port}/15"
        print("[ok] isolated integration Redis is ready")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *tests],
            check=False,
        )
        return completed.returncode
    finally:
        if created:
            removed = docker(["rm", "--force", container_name])
            if removed.returncode == 0:
                print("[ok] isolated integration Redis was removed")
            else:
                print("[fail] isolated integration Redis cleanup failed")
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
