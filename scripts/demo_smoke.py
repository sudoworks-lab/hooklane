"""Run the self-cleaning README Compose demonstration."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Never, cast
from urllib.request import Request, urlopen

import local_e2e


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PROJECT = "hooklane-f012"


class DemoSmokeError(RuntimeError):
    """Raised when the public demo contract fails."""


def fail(message: str) -> Never:
    raise DemoSmokeError(message)


def run(
    command: list[str],
    *,
    check: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        text=True,
        timeout=timeout,
    )


def docker_output(*args: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["docker", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        fail("Docker project resource query failed")
    return tuple(line for line in result.stdout.splitlines() if line)


def project_resources() -> dict[str, tuple[str, ...]]:
    label = f"label=com.docker.compose.project={COMPOSE_PROJECT}"
    return {
        "containers": docker_output("ps", "--all", "--filter", label, "--quiet"),
        "networks": docker_output("network", "ls", "--filter", label, "--quiet"),
        "volumes": docker_output("volume", "ls", "--filter", label, "--quiet"),
    }


def require_no_project_resources() -> None:
    resources = project_resources()
    leftovers = {name: values for name, values in resources.items() if values}
    if leftovers:
        fail("Hooklane Compose project resources already exist or remain after cleanup")


def metrics_text() -> str:
    request = Request(f"{local_e2e.API_ORIGIN}/metrics", method="GET")
    with urlopen(request, timeout=3) as response:
        if response.status != 200:
            fail("metrics endpoint did not return 200")
        return cast(bytes, response.read()).decode("utf-8")


def verify_runtime() -> None:
    local_e2e.wait_until_ready()
    live_status, live = local_e2e.request_json("GET", "/health/live")
    ready_status, ready = local_e2e.request_json("GET", "/health/ready")
    if live_status != 200 or live.get("status") != "live":
        fail("liveness contract failed")
    if ready_status != 200 or ready.get("status") != "ready":
        fail("readiness contract failed")

    event_id = local_e2e.submit_event(event_type="compose.demo")
    delivered = local_e2e.wait_until_delivered(event_id)
    if delivered.get("event_id") != event_id:
        fail("status API returned a different event ID")
    if delivered.get("status") != "delivered" or delivered.get("attempt_count") != 1:
        fail("demo event did not reach the expected terminal state")

    metrics = metrics_text()
    for marker in (
        "hooklane_http_requests_total",
        "hooklane_http_request_duration_seconds",
        "hooklane_enqueue_total",
        "hooklane_queue_depth",
    ):
        if marker not in metrics:
            fail(f"metrics endpoint is missing required family: {marker}")
    for forbidden in (
        "event_id=",
        "request_id=",
        "idempotency_key",
        "redis_url",
        "redis_password",
        "credential",
        "cookie",
    ):
        if forbidden in metrics.lower():
            fail("metrics endpoint contains a forbidden sensitive or cardinality field")
    if json.dumps(delivered).count(event_id) != 1:
        fail("status API event identifier contract is inconsistent")
    print("[ok] demo health, 202 acceptance, delivered status, and metrics passed")


def main() -> int:
    passed = True
    owns_resources = False
    try:
        require_no_project_resources()
        run(["bash", "scripts/init.sh"], timeout=300)
        owns_resources = True
        run(["make", "compose-up"], timeout=600)
        verify_runtime()
    except (
        DemoSmokeError,
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        passed = False
        detail = str(error) if isinstance(error, DemoSmokeError) else type(error).__name__
        print(f"[fail] demo smoke: {detail}")
    finally:
        if owns_resources:
            cleanup = run(["make", "compose-down"], check=False, timeout=180)
            if cleanup.returncode != 0:
                passed = False
                print("[fail] demo smoke Compose cleanup failed")
        try:
            require_no_project_resources()
        except DemoSmokeError:
            passed = False
            print("[fail] demo smoke resource cleanup contract failed")

    if passed:
        print("[ok] demo-smoke completed and Hooklane Compose resources were removed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
