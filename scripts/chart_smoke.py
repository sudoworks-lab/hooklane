"""Verify end-to-end delivery through the local kind NodePort."""

from __future__ import annotations

import json
import time
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen


API_ORIGIN = "http://127.0.0.1:18082"


def request_json(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    request = Request(
        f"{API_ORIGIN}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=2) as response:
            parsed: object = json.loads(response.read())
            if not isinstance(parsed, dict):
                raise RuntimeError("API response is not an object")
            return response.status, cast(dict[str, Any], parsed)
    except HTTPError as exc:
        parsed = json.loads(exc.read())
        if not isinstance(parsed, dict):
            raise RuntimeError("API error response is not an object") from None
        return exc.code, cast(dict[str, Any], parsed)


def wait_ready() -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            status, _response = request_json("GET", "/health/ready")
        except OSError:
            time.sleep(0.2)
            continue
        if status == 200:
            return
        time.sleep(0.2)
    raise RuntimeError("kind API did not become ready")


def main() -> int:
    wait_ready()
    status, accepted = request_json(
        "POST",
        "/v1/events",
        {"event_type": "kind.smoke", "payload": {}},
    )
    event_id = accepted.get("event_id")
    if status != 202 or not isinstance(event_id, str):
        raise RuntimeError("kind API did not accept the event")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, record = request_json("GET", f"/v1/events/{event_id}")
        if status == 200 and record.get("status") == "delivered":
            if record.get("event_id") != event_id or record.get("attempt_count") != 1:
                raise RuntimeError("delivered event state is inconsistent")
            print(f"[ok] kind chart delivered event {event_id}")
            return 0
        time.sleep(0.2)
    raise RuntimeError("kind event did not reach delivered status")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"[fail] chart smoke: {exc}")
        raise SystemExit(1) from None
