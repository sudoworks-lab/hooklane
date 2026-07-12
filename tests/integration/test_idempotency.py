from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import os
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.api.app import create_app
from hooklane.queue.events import RedisEventStore


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    url = os.environ.get("HOOKLANE_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
    client = Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        try:
            await client.ping()
        except RedisError:
            pytest.fail("A dedicated Redis test instance is required")
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_equivalent_requests_enqueue_once(redis_client: Redis) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    idempotency_key = f"delivery-{uuid4().hex}"
    headers = {"Idempotency-Key": idempotency_key}
    first_request = {
        "event_type": "delivery.test",
        "payload": {"attempt": 1, "metadata": {"alpha": True, "beta": "two"}},
    }
    equivalent_request = {
        "payload": {"metadata": {"beta": "two", "alpha": True}, "attempt": 1},
        "event_type": "delivery.test",
    }
    event_id: UUID | None = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(event_store=store)),
            base_url="http://test",
        ) as client:
            first, second = await asyncio.gather(
                client.post("/v1/events", json=first_request, headers=headers),
                client.post("/v1/events", json=equivalent_request, headers=headers),
            )

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["event_id"] == second.json()["event_id"]
        event_id = UUID(first.json()["event_id"])

        stream_records = await redis_client.xrange(store.stream_key)
        idempotency_record = await redis_client.hgetall(
            store.idempotency_key(idempotency_key)
        )
        assert stream_records is not None
        assert len(stream_records) == 1
        assert idempotency_record["event_id"] == str(event_id)
        assert len(idempotency_record["fingerprint"]) == 64
        assert idempotency_key not in store.idempotency_key(idempotency_key)
    finally:
        keys = [store.stream_key, store.idempotency_key(idempotency_key)]
        if event_id is not None:
            keys.append(store.status_key(event_id))
        await redis_client.delete(*keys)


@pytest.mark.asyncio
async def test_same_key_with_different_content_returns_fixed_conflict(
    redis_client: Redis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    idempotency_key = f"delivery-{uuid4().hex}"
    original_marker = uuid4().hex
    conflicting_marker = uuid4().hex
    headers = {"Idempotency-Key": idempotency_key}
    event_id: UUID | None = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(event_store=store)),
            base_url="http://test",
        ) as client:
            accepted = await client.post(
                "/v1/events",
                json={"event_type": "delivery.test", "payload": {"value": original_marker}},
                headers=headers,
            )
            conflict = await client.post(
                "/v1/events",
                json={
                    "event_type": "delivery.test",
                    "payload": {"value": conflicting_marker},
                },
                headers=headers,
            )

        assert accepted.status_code == 202
        event_id = UUID(accepted.json()["event_id"])
        assert conflict.status_code == 409
        assert conflict.json() == {
            "detail": "Idempotency-Key conflicts with an existing request"
        }
        stream_records = await redis_client.xrange(store.stream_key)
        assert stream_records is not None
        assert len(stream_records) == 1
        assert original_marker not in conflict.text
        assert conflicting_marker not in conflict.text
        assert idempotency_key not in conflict.text
        assert original_marker not in caplog.text
        assert conflicting_marker not in caplog.text
        assert idempotency_key not in caplog.text
    finally:
        keys = [store.stream_key, store.idempotency_key(idempotency_key)]
        if event_id is not None:
            keys.append(store.status_key(event_id))
        await redis_client.delete(*keys)
