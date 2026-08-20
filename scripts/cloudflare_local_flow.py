"""Run a credential-free Cloudflare Worker, D1, Queue, and mock-sink flow locally."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
CLOUDFLARE = ROOT / "cloudflare"
WORKER_ORIGIN = "http://127.0.0.1:18787"
SINK_ORIGIN = "http://127.0.0.1:18081"
D1_ROW_LIMIT_BYTES = 2_000_000


class FlowError(RuntimeError):
    """Raised when the local Cloudflare contract is not observed."""


def assert_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise FlowError(f"required local port is already in use: {port}") from exc


def request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    body = None
    request_headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = Request(
        f"{WORKER_ORIGIN}{path}",
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            return response.status, json.loads(response_body or b"{}")
    except HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def wait_for_worker(deadline_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            status_code, body = request_json("GET", "/health/live", timeout=1.0)
        except OSError:
            time.sleep(0.1)
            continue
        if status_code == 200 and body == {"status": "live"}:
            return
        time.sleep(0.1)
    raise FlowError("local Worker did not become live")


def wait_for_sink(deadline_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{SINK_ORIGIN}/health/live", timeout=1.0) as response:
                if response.status == 204:
                    return
        except OSError:
            time.sleep(0.1)
            continue
    raise FlowError("local mock sink did not become live")


def wait_for_status(event_id: str, expected: str, deadline_seconds: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status_code, last = request_json("GET", f"/v1/events/{event_id}")
        if status_code == 200 and last.get("status") == expected:
            return last
        time.sleep(0.1)
    raise FlowError(f"event did not reach {expected}: {last}")


def post_event(
    idempotency_key: str,
    *,
    payload: dict[str, Any] | None = None,
    fault: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Idempotency-Key": idempotency_key}
    if fault is not None:
        headers["X-Hooklane-Spike-Fault"] = fault
    return request_json(
        "POST",
        "/v1/events",
        payload={
            "event_type": "delivery.test",
            "payload": payload or {"message": "accepted"},
        },
        headers=headers,
    )


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_contract() -> dict[str, Any]:
    assert_port_available(18081)
    assert_port_available(18787)
    with tempfile.TemporaryDirectory(prefix="hooklane-cloudflare-flow-") as temporary:
        node_install = subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=CLOUDFLARE,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        if node_install.returncode != 0:
            raise FlowError("locked local Wrangler installation failed")
        persistence = Path(temporary) / "state"
        persistence.mkdir()
        migration = subprocess.run(
            [
                "uv",
                "run",
                "pywrangler",
                "d1",
                "migrations",
                "apply",
                "hooklane-spike-db",
                "--local",
                "--persist-to",
                str(persistence),
            ],
            cwd=CLOUDFLARE,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        if migration.returncode != 0:
            raise FlowError("local D1 migration failed")

        sink_log = Path(temporary) / "sink.log"
        worker_log = Path(temporary) / "worker.log"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        with sink_log.open("wb") as sink_output, worker_log.open("wb") as worker_output:
            sink = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "hooklane.mock_sink.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18081",
                    "--log-level",
                    "warning",
                ],
                cwd=ROOT,
                env=environment,
                stdout=sink_output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            worker = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "pywrangler",
                    "dev",
                    "--local",
                    "--port",
                    "18787",
                    "--persist-to",
                    str(persistence),
                    "--show-interactive-dev-session=false",
                ],
                cwd=CLOUDFLARE,
                stdout=worker_output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                wait_for_worker()
                default_repair_code, _default_repair = request_json(
                    "POST", "/__spike/repair"
                )
                default_regression_code, _default_regression = request_json(
                    "POST",
                    "/__spike/delivery-regression",
                    payload={"scenario": "delivered_redelivery"},
                )
                if default_repair_code != 404 or default_regression_code != 404:
                    raise FlowError(
                        "local-only interfaces were reachable with default test mode: "
                        f"repair={default_repair_code} regression={default_regression_code}"
                    )
                stop_process(worker)
                worker = subprocess.Popen(
                    [
                        "uv",
                        "run",
                        "pywrangler",
                        "dev",
                        "--local",
                        "--port",
                        "18787",
                        "--persist-to",
                        str(persistence),
                        "--var",
                        "SPIKE_TEST_MODE:true",
                        "--show-interactive-dev-session=false",
                    ],
                    cwd=CLOUDFLARE,
                    stdout=worker_output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                wait_for_worker()
                wait_for_sink()
                ready_code, ready = request_json("GET", "/health/ready")
                if (ready_code, ready) != (200, {"status": "ready"}):
                    raise FlowError(f"dependency readiness failed: {ready_code} {ready}")

                missing_code, _missing = request_json("GET", f"/v1/events/{uuid4()}")
                if missing_code != 404:
                    raise FlowError(f"unknown event did not return 404: {missing_code}")

                normal_code, normal = post_event("local-normal")
                if normal_code != 202 or normal.get("status") != "queued":
                    raise FlowError(f"normal acceptance failed: {normal_code} {normal}")
                normal_id = str(normal["event_id"])
                normal_final = wait_for_status(normal_id, "delivered")
                if normal_final["attempt_count"] != 1:
                    raise FlowError(f"normal attempt parity failed: {normal_final}")

                reuse_code, reuse = post_event("local-normal")
                if (
                    reuse_code != 202
                    or reuse.get("event_id") != normal_id
                    or reuse.get("status") != "queued"
                ):
                    raise FlowError(f"idempotent reuse failed: {reuse_code} {reuse}")
                time.sleep(1.25)
                reused_final = wait_for_status(normal_id, "delivered")
                if reused_final["attempt_count"] != 1:
                    raise FlowError(f"idempotent reuse re-enqueued: {reused_final}")

                with ThreadPoolExecutor(max_workers=12) as executor:
                    concurrent = list(
                        executor.map(lambda _index: post_event("local-concurrent"), range(20))
                    )
                if {code for code, _body in concurrent} != {202}:
                    raise FlowError("concurrent idempotency returned a non-202 response")
                concurrent_ids = {str(body["event_id"]) for _code, body in concurrent}
                if len(concurrent_ids) != 1:
                    raise FlowError(f"concurrent idempotency created multiple events: {concurrent_ids}")
                concurrent_id = concurrent_ids.pop()
                concurrent_final = wait_for_status(concurrent_id, "delivered")
                if concurrent_final["attempt_count"] != 1:
                    raise FlowError(f"concurrent idempotency duplicated enqueue: {concurrent_final}")

                conflict_code, _conflict = post_event(
                    "local-normal",
                    payload={"message": "different"},
                )
                if conflict_code != 409:
                    raise FlowError(f"idempotency conflict did not return 409: {conflict_code}")

                regression_expectations = {
                    "dead_letter_redelivery": {
                        "attempt_count": 5,
                        "result": "dead_letter",
                        "sink_calls": 5,
                        "status": "dead_letter",
                    },
                    "delivered_redelivery": {
                        "attempt_count": 1,
                        "result": "delivered",
                        "sink_calls": 1,
                        "status": "delivered",
                    },
                    "concurrent_stale_failure": {
                        "attempt_count": 2,
                        "newer_result": "delivered",
                        "sink_calls": 2,
                        "stale_result": "delivered",
                        "status": "delivered",
                    },
                    "stale_success": {
                        "attempt_count": 2,
                        "newer_result": "retry_scheduled",
                        "sink_calls": 2,
                        "stale_result": "retry_scheduled",
                        "status": "retry_scheduled",
                    },
                    "delivery_transition_failure": {
                        "attempt_count": 2,
                        "result": "delivered",
                        "sink_calls": 2,
                        "status": "delivered",
                        "transition_failed": True,
                    },
                }
                regression_results: dict[str, dict[str, Any]] = {}
                for scenario, expected in regression_expectations.items():
                    regression_code, regression = request_json(
                        "POST",
                        "/__spike/delivery-regression",
                        payload={"scenario": scenario},
                    )
                    if regression_code != 200 or any(
                        regression.get(key) != value for key, value in expected.items()
                    ):
                        raise FlowError(
                            f"delivery regression failed for {scenario}: "
                            f"{regression_code} {regression}"
                        )
                    regression_results[scenario] = {
                        key: regression[key] for key in expected
                    }

                failed_code, _failed = post_event("local-d1-failure", fault="d1_persistence")
                if failed_code != 503:
                    raise FlowError(f"D1 failure returned false acceptance: {failed_code}")

                payload_fault_code, _payload_fault = post_event(
                    "local-payload-chunk-failure",
                    payload={"data": "x" * (D1_ROW_LIMIT_BYTES + 64 * 1024)},
                    fault="payload_chunk_persistence",
                )
                if payload_fault_code != 503:
                    raise FlowError(
                        f"payload chunk failure returned false acceptance: {payload_fault_code}"
                    )
                payload_recovery_code, payload_recovery = post_event(
                    "local-payload-chunk-failure"
                )
                if payload_recovery_code != 202:
                    raise FlowError(
                        "payload chunk rollback retained conflicting acceptance state"
                    )
                wait_for_status(str(payload_recovery["event_id"]), "delivered")

                queue_code, queue_failed = post_event("local-queue-failure", fault="queue_send")
                if queue_code != 202:
                    raise FlowError(f"durable outbox acceptance failed: {queue_code}")
                queue_id = str(queue_failed["event_id"])
                outbox_code, outbox = request_json("GET", f"/__spike/outbox/{queue_id}")
                if (outbox_code, outbox.get("state")) != (200, "pending"):
                    raise FlowError(f"Queue failure did not retain pending outbox: {outbox}")
                with ThreadPoolExecutor(max_workers=12) as executor:
                    repairs = list(
                        executor.map(
                            lambda _index: request_json("POST", "/__spike/repair"),
                            range(20),
                        )
                    )
                if {code for code, _body in repairs} != {200}:
                    raise FlowError(f"concurrent outbox repair failed: {repairs}")
                repair_dispatches = sum(
                    int(body.get("repaired", 0)) for _code, body in repairs
                )
                if repair_dispatches != 1:
                    raise FlowError(
                        f"concurrent repair did not claim one owner: {repair_dispatches}"
                    )
                wait_for_status(queue_id, "delivered")
                repaired_outbox_code, repaired_outbox = request_json(
                    "GET", f"/__spike/outbox/{queue_id}"
                )
                if (
                    repaired_outbox_code != 200
                    or repaired_outbox.get("state") != "sent"
                    or repaired_outbox.get("send_attempt_count") != 1
                ):
                    raise FlowError(f"concurrent repair lost outbox state: {repaired_outbox}")

                transition_code, transition_failed = post_event(
                    "local-transition-failure",
                    fault="dispatch_transition",
                )
                if transition_code != 202:
                    raise FlowError(f"transition fault acceptance failed: {transition_code}")
                transition_id = str(transition_failed["event_id"])
                wait_for_status(transition_id, "delivered")
                transition_outbox_code, transition_outbox = request_json(
                    "GET", f"/__spike/outbox/{transition_id}"
                )
                if (transition_outbox_code, transition_outbox.get("state")) != (200, "pending"):
                    raise FlowError(f"transition fault lost repair state: {transition_outbox}")
                repair_code, _repair = request_json("POST", "/__spike/repair")
                if repair_code != 200:
                    raise FlowError("transition fault repair request failed")
                time.sleep(1.25)
                duplicate_final = wait_for_status(transition_id, "delivered")
                if duplicate_final["attempt_count"] != 1:
                    raise FlowError(f"terminal duplicate was not suppressed: {duplicate_final}")

                large_payload = {"data": "x" * (D1_ROW_LIMIT_BYTES + 64 * 1024)}
                large_code, large = post_event("local-large-payload", payload=large_payload)
                if large_code != 202:
                    raise FlowError(f"large payload was rejected before reference enqueue: {large_code}")
                large_final = wait_for_status(str(large["event_id"]), "delivered")

                stop_process(sink)
                failure_environment = environment.copy()
                failure_environment["HOOKLANE_MOCK_SINK_MODE"] = "server_error"
                sink = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "hooklane.mock_sink.app:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "18081",
                        "--log-level",
                        "warning",
                    ],
                    cwd=ROOT,
                    env=failure_environment,
                    stdout=sink_output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                wait_for_sink()
                retry_code, retry = post_event("local-retryable-5xx")
                if retry_code != 202:
                    raise FlowError(f"retryable failure acceptance failed: {retry_code}")
                retry_final = wait_for_status(str(retry["event_id"]), "dead_letter", 40.0)
                if retry_final["attempt_count"] != 5:
                    raise FlowError(f"maximum attempts parity failed: {retry_final}")

                return {
                    "concurrent_logical_events": len(concurrent_ids) + 1,
                    "delivery_regressions": regression_results,
                    "d1_failure_status": failed_code,
                    "default_test_interfaces_status": 404,
                    "duplicate_boundary_attempt_count": duplicate_final["attempt_count"],
                    "idempotency_conflict_status": conflict_code,
                    "large_payload_status": large_final["status"],
                    "large_payload_bytes": len(large_payload["data"]),
                    "normal_attempt_count": normal_final["attempt_count"],
                    "normal_status": normal_final["status"],
                    "outbox_concurrent_repair_dispatches": repair_dispatches,
                    "outbox_send_attempt_count": repaired_outbox["send_attempt_count"],
                    "outbox_repair_status": "delivered",
                    "payload_chunk_failure_status": payload_fault_code,
                    "readiness_status": ready_code,
                    "retryable_5xx_attempt_count": retry_final["attempt_count"],
                    "retryable_5xx_status": retry_final["status"],
                }
            except Exception as error:
                worker_output.flush()
                tail = worker_log.read_text(encoding="utf-8", errors="replace")[-6000:]
                raise FlowError(f"{error}\nworker log tail:\n{tail}") from error
            finally:
                stop_process(worker)
                stop_process(sink)


def main() -> int:
    try:
        evidence = run_contract()
    except (FlowError, OSError, subprocess.SubprocessError) as error:
        print(f"[fail] Cloudflare local flow: {error}")
        return 1
    print("[pass] Cloudflare local flow")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
