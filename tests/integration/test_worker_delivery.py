from __future__ import annotations

from collections.abc import AsyncIterator
import os
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport
from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.delivery.sink import MockSinkClient
from hooklane.domain.events import EventRequest
from hooklane.mock_sink.app import MockSinkMode, MockSinkReceipts, create_app
from hooklane.queue.events import RedisEventStore
from hooklane.worker.service import EventWorker, WorkerResult


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
async def test_worker_acknowledges_only_after_successful_delivery(redis_client: Redis) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()
    receipts = MockSinkReceipts()
    sink = MockSinkClient(transport=ASGITransport(app=create_app(receipts=receipts)))
    worker = EventWorker(store, sink)

    try:
        await store.enqueue(
            event_id,
            EventRequest(event_type="delivery.test", payload={"message": "accepted"}),
        )
        result = await worker.run_once()

        status_record = await redis_client.hgetall(store.status_key(event_id))
        pending = await redis_client.xpending_range(
            store.stream_key,
            "hooklane-workers",
            min="-",
            max="+",
            count=10,
        )

        assert result is WorkerResult.DELIVERED
        assert status_record["status"] == "delivered"
        assert status_record["attempt_count"] == "1"
        assert pending == []
        assert receipts.event_ids == frozenset({event_id})
    finally:
        await sink.close()
        await redis_client.delete(store.stream_key, store.status_key(event_id))


@pytest.mark.asyncio
async def test_worker_leaves_failed_delivery_pending(redis_client: Redis) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = UUID(uuid4().hex)
    receipts = MockSinkReceipts()
    sink = MockSinkClient(
        transport=ASGITransport(
            app=create_app(receipts=receipts, mode=MockSinkMode.SERVER_ERROR)
        )
    )
    worker = EventWorker(store, sink)

    try:
        await store.enqueue(
            event_id,
            EventRequest(event_type="delivery.test", payload={"message": "retry-later"}),
        )
        result = await worker.run_once()

        status_record = await redis_client.hgetall(store.status_key(event_id))
        pending = await redis_client.xpending_range(
            store.stream_key,
            "hooklane-workers",
            min="-",
            max="+",
            count=10,
        )

        assert result is WorkerResult.FAILED_PENDING
        assert status_record["status"] == "delivering"
        assert status_record["attempt_count"] == "1"
        assert len(pending) == 1
        assert receipts.event_ids == frozenset()
    finally:
        await sink.close()
        await redis_client.delete(store.stream_key, store.status_key(event_id))
