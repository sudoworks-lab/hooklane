from __future__ import annotations

from collections.abc import AsyncIterator
import os
from typing import cast
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport
from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.delivery.sink import MockSinkClient
from hooklane.domain.events import EventRequest
from hooklane.mock_sink.app import MockSinkReceipts, create_app
from hooklane.queue.events import QueuedEvent, RedisEventStore
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


class CountingSink:
    def __init__(self, delegate: MockSinkClient) -> None:
        self.delegate = delegate
        self.attempts = 0

    async def deliver(self, queued_event: QueuedEvent) -> None:
        self.attempts += 1
        await self.delegate.deliver(queued_event)


@pytest.mark.asyncio
async def test_stale_pending_message_is_claimed_and_redelivered(
    redis_client: Redis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()
    payload_marker = uuid4().hex
    receipts = MockSinkReceipts()
    client = MockSinkClient(transport=ASGITransport(app=create_app(receipts=receipts)))
    sink = CountingSink(client)

    try:
        await store.enqueue(
            event_id,
            EventRequest(
                event_type="delivery.test",
                payload={"marker": payload_marker},
            ),
        )
        await store.ensure_consumer_group("hooklane-workers")
        queued_event = await store.read_next("hooklane-workers", "worker-1")
        assert queued_event is not None
        assert await store.mark_delivery_started(queued_event) == 1

        # The sink accepted the event, but worker-1 stopped before acknowledging it.
        await sink.deliver(queued_event)
        crashed_status = await redis_client.hgetall(store.status_key(event_id))
        crashed_pending = await redis_client.xpending_range(
            store.stream_key,
            "hooklane-workers",
            min="-",
            max="+",
            count=10,
        )
        assert crashed_status["status"] == "delivering"
        assert crashed_status["attempt_count"] == "1"
        assert len(crashed_pending) == 1
        assert crashed_pending[0]["consumer"] == "worker-1"

        recovering_worker = EventWorker(
            store,
            sink,
            consumer_name="worker-2",
            pending_idle_ms=0,
        )
        assert await recovering_worker.run_once() is WorkerResult.DELIVERED

        recovered_status = await redis_client.hgetall(store.status_key(event_id))
        recovered_pending = await redis_client.xpending_range(
            store.stream_key,
            "hooklane-workers",
            min="-",
            max="+",
            count=10,
        )
        stream_records = cast(
            list[tuple[str, dict[str, str]]],
            await redis_client.xrange(store.stream_key),
        )

        assert recovered_status["status"] == "delivered"
        assert recovered_status["attempt_count"] == "2"
        assert recovered_pending == []
        assert len(stream_records) == 1
        assert stream_records[0][1]["event_id"] == str(event_id)
        assert receipts.event_ids == frozenset({event_id})
        assert sink.attempts == 2
        assert payload_marker not in caplog.text
    finally:
        await client.close()
        await redis_client.delete(store.stream_key, store.status_key(event_id))
