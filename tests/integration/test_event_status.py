from __future__ import annotations

from collections.abc import AsyncIterator
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.api.app import create_app
from hooklane.domain.events import EventStatus
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
async def test_get_event_status_exposes_all_public_states(
    redis_client: Redis,
    delivery_status: EventStatus,
    attempt_count: int,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()
    internal_stream_id = "1720000000000-0"
    await redis_client.hset(
        store.status_key(event_id),
        mapping={
            "event_id": str(event_id),
            "status": delivery_status.value,
            "attempt_count": str(attempt_count),
            "stream_id": internal_stream_id,
        },
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(event_store=store)),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/v1/events/{event_id}")

        assert response.status_code == 200
        assert response.json() == {
            "event_id": str(event_id),
            "status": delivery_status.value,
            "attempt_count": attempt_count,
        }
        assert internal_stream_id not in response.text
        assert store.status_key(event_id) not in response.text
    finally:
        await redis_client.delete(store.status_key(event_id))


@pytest.mark.asyncio
async def test_get_unknown_event_returns_fixed_404_without_redis_key(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=create_app(event_store=store)),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/v1/events/{event_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}
    assert store.status_key(event_id) not in response.text
