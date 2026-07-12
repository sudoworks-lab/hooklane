"""Exercise the running local Compose API without printing event payloads."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen


API_ORIGIN = "http://127.0.0.1:18080"


def request_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = Request(
        f"{API_ORIGIN}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=2) as response:
            content = response.read()
            parsed: object = json.loads(content) if content else {}
            if not isinstance(parsed, dict):
                raise RuntimeError("local API response is not an object")
            return response.status, cast(dict[str, Any], parsed)
    except HTTPError as exc:
        content = exc.read()
        parsed = json.loads(content) if content else {}
        if not isinstance(parsed, dict):
            raise RuntimeError("local API error response is not an object") from None
        return exc.code, cast(dict[str, Any], parsed)


def wait_until_ready() -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            status, _response = request_json("GET", "/health/ready")
        except OSError:
            time.sleep(0.1)
            continue
        if status == 200:
            return
        time.sleep(0.1)
    raise RuntimeError("local API did not become ready")


def wait_until_delivered(event_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status, response = request_json("GET", f"/v1/events/{event_id}")
        if status == 200 and response.get("status") == "delivered":
            return response
        time.sleep(0.1)
    raise RuntimeError("event did not reach delivered status")


def submit_event(*, event_type: str, idempotency_key: str | None = None) -> str:
    headers = {} if idempotency_key is None else {"Idempotency-Key": idempotency_key}
    status, response = request_json(
        "POST",
        "/v1/events",
        body={"event_type": event_type, "payload": {}},
        headers=headers,
    )
    if status != 202 or not isinstance(response.get("event_id"), str):
        raise RuntimeError("event acceptance contract failed")
    return cast(str, response["event_id"])


def run_smoke() -> None:
    event_id = submit_event(event_type="compose.smoke")
    result = wait_until_delivered(event_id)
    if result.get("event_id") != event_id or result.get("attempt_count") != 1:
        raise RuntimeError("delivered event state is inconsistent")
    print(f"[ok] compose smoke delivered event {event_id}")


def run_idempotency() -> None:
    key = "hooklane-compose-e2e"
    first_id = submit_event(event_type="compose.idempotency", idempotency_key=key)
    second_id = submit_event(event_type="compose.idempotency", idempotency_key=key)
    if first_id != second_id:
        raise RuntimeError("idempotent requests returned different event IDs")
    conflict_status, _response = request_json(
        "POST",
        "/v1/events",
        body={"event_type": "compose.conflict", "payload": {}},
        headers={"Idempotency-Key": key},
    )
    if conflict_status != 409:
        raise RuntimeError("idempotency conflict did not return 409")
    wait_until_delivered(first_id)
    print(f"[ok] compose idempotency contract passed for event {first_id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=("smoke", "idempotency"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    wait_until_ready()
    if args.scenario == "smoke":
        run_smoke()
    else:
        run_idempotency()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"[fail] local E2E: {exc}")
        raise SystemExit(1) from None
