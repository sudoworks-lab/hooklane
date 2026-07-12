from __future__ import annotations

from collections.abc import AsyncIterator
import os
from typing import cast
from uuid import uuid4

from httpx import MockTransport, Request, Response
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.delivery.dead_letter import DeadLetterPolicy
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
async def test_non_retryable_failure_moves_to_dead_letter_once(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()
    delivery_count = 0

    def reject(request: Request) -> Response:
        nonlocal delivery_count
        delivery_count += 1
        return Response(400, request=request)

    sink = MockSinkClient(transport=MockTransport(reject))
    worker = EventWorker(
        store,
        sink,
        retry_policy=RetryPolicy(),
        dead_letter_policy=DeadLetterPolicy(maximum_attempts=3),
    )

    try:
        await store.enqueue(
            event_id,
            EventRequest(event_type="delivery.test", payload={"message": "terminal"}),
        )

        assert await worker.run_once() is WorkerResult.DEAD_LETTERED
        assert await worker.run_once() is WorkerResult.NO_MESSAGE

        status = await redis_client.hgetall(store.status_key(event_id))
        dead_letters = cast(
            list[tuple[str, dict[str, str]]],
            await redis_client.xrange(store.dead_letter_key),
        )
        pending = await redis_client.xpending_range(
            store.stream_key,
            "hooklane-workers",
            min="-",
            max="+",
            count=10,
        )

        assert status["status"] == "dead_letter"
        assert status["attempt_count"] == "1"
        assert status["last_error_class"] == "http_4xx"
        assert len(dead_letters) == 1
        assert dead_letters[0][1]["event_id"] == str(event_id)
        assert dead_letters[0][1]["error_class"] == "http_4xx"
        assert dead_letters[0][1]["attempt_count"] == "1"
        assert pending == []
        assert await redis_client.zcard(store.retry_schedule_key) == 0
        assert delivery_count == 1
    finally:
        await sink.close()
        await redis_client.delete(
            store.stream_key,
            store.status_key(event_id),
            store.dead_letter_key,
            store.retry_schedule_key,
        )


@pytest.mark.asyncio
async def test_retryable_failure_moves_to_dead_letter_at_attempt_limit(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()
    clock = [1_000.0]
    delivery_count = 0

    def reject(request: Request) -> Response:
        nonlocal delivery_count
        delivery_count += 1
        return Response(503, request=request)

    sink = MockSinkClient(transport=MockTransport(reject))
    worker = EventWorker(
        store,
        sink,
        retry_policy=RetryPolicy(
            base_delay_seconds=1,
            maximum_delay_seconds=1,
            jitter_ratio=0,
        ),
        dead_letter_policy=DeadLetterPolicy(maximum_attempts=2),
        clock=lambda: clock[0],
    )

    try:
        await store.enqueue(
            event_id,
            EventRequest(event_type="delivery.test", payload={"message": "limited"}),
        )

        assert await worker.run_once() is WorkerResult.RETRY_SCHEDULED
        clock[0] = 1_001.0
        assert await worker.run_once() is WorkerResult.DEAD_LETTERED
        assert await worker.run_once() is WorkerResult.NO_MESSAGE

        status = await redis_client.hgetall(store.status_key(event_id))
        dead_letters = cast(
            list[tuple[str, dict[str, str]]],
            await redis_client.xrange(store.dead_letter_key),
        )
        pending = await redis_client.xpending_range(
            store.stream_key,
            "hooklane-workers",
            min="-",
            max="+",
            count=10,
        )

        assert status["status"] == "dead_letter"
        assert status["attempt_count"] == "2"
        assert status["last_error_class"] == "http_5xx"
        assert len(dead_letters) == 1
        assert dead_letters[0][1]["event_id"] == str(event_id)
        assert dead_letters[0][1]["error_class"] == "http_5xx"
        assert dead_letters[0][1]["attempt_count"] == "2"
        assert pending == []
        assert await redis_client.zcard(store.retry_schedule_key) == 0
        assert delivery_count == 2
    finally:
        await sink.close()
        await redis_client.delete(
            store.stream_key,
            store.status_key(event_id),
            store.dead_letter_key,
            store.retry_schedule_key,
        )
