from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hooklane.api.app import create_app
from hooklane.domain.events import EventRequest, EventStatus
from hooklane.queue.events import EventStatusRecord


class AcceptingEventStore:
    def __init__(self, status_record: EventStatusRecord | None = None) -> None:
        self._status_record = status_record

    async def enqueue(self, _event_id: UUID, _event: EventRequest) -> None:
        pass

    async def enqueue_idempotent(
        self,
        event_id: UUID,
        _event: EventRequest,
        _idempotency_key: str,
    ) -> UUID:
        return event_id

    async def get_status(self, _event_id: UUID) -> EventStatusRecord | None:
        return self._status_record


def create_test_app(status_record: EventStatusRecord | None = None) -> FastAPI:
    return create_app(event_store=AcceptingEventStore(status_record))


@pytest.mark.asyncio
async def test_post_event_returns_202_unique_id_and_initial_status() -> None:
    request = {"event_type": "delivery.test", "payload": {}}

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app()),
        base_url="http://test",
    ) as client:
        first = await client.post("/v1/events", json=request)
        second = await client.post("/v1/events", json=request)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["status"] == "queued"
    assert set(first.json()) == {"event_id", "status"}
    assert UUID(first.json()["event_id"])
    assert first.json()["event_id"] != second.json()["event_id"]


@pytest.mark.asyncio
async def test_post_event_rejects_malformed_json_without_reflecting_input() -> None:
    invalid_body = "not-json"

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/events",
            content=invalid_body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid JSON request body"}
    assert invalid_body not in response.text


@pytest.mark.asyncio
async def test_post_event_rejects_missing_field_without_reflecting_payload() -> None:
    marker = uuid4().hex

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.post("/v1/events", json={"payload": {marker: marker}})

    assert response.status_code == 422
    assert response.json() == {"detail": "Request validation failed"}
    assert marker not in response.text


@pytest.mark.asyncio
async def test_post_event_does_not_log_or_return_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = uuid4().hex

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/events",
            json={"event_type": "delivery.test", "payload": {marker: marker}},
        )

    assert response.status_code == 202
    assert marker not in response.text
    assert marker not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_status", "attempt_count"),
    [
        (EventStatus.QUEUED, 0),
        (EventStatus.DELIVERING, 1),
        (EventStatus.RETRY_SCHEDULED, 2),
        (EventStatus.DELIVERED, 3),
        (EventStatus.DEAD_LETTER, 4),
    ],
)
async def test_get_event_returns_public_status(
    delivery_status: EventStatus,
    attempt_count: int,
) -> None:
    event_id = uuid4()
    event_status = EventStatusRecord(
        event_id=event_id,
        status=delivery_status,
        attempt_count=attempt_count,
    )

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app(event_status)),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/v1/events/{event_id}")

    assert response.status_code == 200
    assert response.json() == {
        "event_id": str(event_id),
        "status": delivery_status.value,
        "attempt_count": attempt_count,
    }


@pytest.mark.asyncio
async def test_get_unknown_event_returns_fixed_404() -> None:
    event_id = uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app()),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/v1/events/{event_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}
