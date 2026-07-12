from __future__ import annotations

from collections.abc import AsyncIterator
from random import Random
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import MockTransport, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.delivery.retry import RetryPolicy
from hooklane.delivery.sink import MockSinkClient
from hooklane.domain.events import EventRequest
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
async def test_scheduled_retry_is_persisted_and_redelivered(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()
    clock = [1_000.0]
    delivery_count = 0

    def deliver(request: Request) -> Response:
        nonlocal delivery_count
        delivery_count += 1
        return Response(503 if delivery_count == 1 else 204, request=request)

    sink = MockSinkClient(transport=MockTransport(deliver))
    policy = RetryPolicy(
        base_delay_seconds=2,
        maximum_delay_seconds=8,
        jitter_ratio=0,
    )
    worker = EventWorker(
        store,
        sink,
        retry_policy=policy,
        clock=lambda: clock[0],
        random_source=Random(7),
    )

    try:
        await store.enqueue(
            event_id,
            EventRequest(event_type="delivery.test", payload={"message": "retry"}),
        )

        first_result = await worker.run_once()
        scheduled_status = await redis_client.hgetall(store.status_key(event_id))
        due_at = await redis_client.zscore(store.retry_schedule_key, str(event_id))

        assert first_result is WorkerResult.RETRY_SCHEDULED
        assert scheduled_status["status"] == "retry_scheduled"
        assert scheduled_status["attempt_count"] == "1"
        assert scheduled_status["last_error_class"] == "http_5xx"
        assert due_at == 1_002_000

        clock[0] = 1_001.999
        assert await worker.run_once() is WorkerResult.NO_MESSAGE

        clock[0] = 1_002.0
        restarted_worker = EventWorker(
            store,
            sink,
            consumer_name="worker-2",
            retry_policy=policy,
            clock=lambda: clock[0],
            random_source=Random(7),
        )
        retry_result = await restarted_worker.run_once()
        delivered_status = await redis_client.hgetall(store.status_key(event_id))
        pending = await redis_client.xpending_range(
            store.stream_key,
            "hooklane-workers",
            min="-",
            max="+",
            count=10,
        )

        assert retry_result is WorkerResult.DELIVERED
        assert delivered_status["status"] == "delivered"
        assert delivered_status["attempt_count"] == "2"
        assert "last_error_class" not in delivered_status
        assert await redis_client.zcard(store.retry_schedule_key) == 0
        assert pending == []
        assert delivery_count == 2
    finally:
        await sink.close()
        await redis_client.delete(
            store.stream_key,
            store.status_key(event_id),
            store.retry_schedule_key,
        )
