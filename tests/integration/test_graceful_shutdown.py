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
from hooklane.domain.events import EventRequest
from hooklane.queue.events import EventStatusRecord, QueuedEvent, RedisEventStore
from hooklane.runtime import ServiceHealth
from hooklane.worker.runtime import WorkerRuntime
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


class SlowStore:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def enqueue(self, _event_id: UUID, _event: EventRequest) -> None:
        self.started.set()
        await self.release.wait()

    async def enqueue_idempotent(
        self,
        event_id: UUID,
        _event: EventRequest,
        _idempotency_key: str,
    ) -> UUID:
        await self.enqueue(event_id, _event)
        return event_id

    async def get_status(self, _event_id: UUID) -> EventStatusRecord | None:
        return None


class BlockingSink:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.event_ids: list[UUID] = []

    async def deliver(self, queued_event: QueuedEvent) -> None:
        self.started.set()
        await self.release.wait()
        self.event_ids.append(queued_event.event_id)


@pytest.mark.asyncio
async def test_api_shutdown_stops_new_requests_and_drains_current_request() -> None:
    store = SlowStore()
    health = ServiceHealth(started=True, dependency_ready=True)
    application = create_app(event_store=store, health=health)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        current_request = asyncio.create_task(
            client.post(
                "/v1/events",
                json={"event_type": "delivery.test", "payload": {}},
            )
        )
        await store.started.wait()

        health.begin_shutdown()
        rejected = await client.post(
            "/v1/events",
            json={"event_type": "delivery.test", "payload": {}},
        )
        assert rejected.status_code == 503

        store.release.set()
        assert (await current_request).status_code == 202
        assert await health.wait_for_drain(timeout_seconds=1)


@pytest.mark.asyncio
async def test_worker_shutdown_finishes_in_flight_message_before_ack(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()
    sink = BlockingSink()
    health = ServiceHealth()
    worker = EventWorker(store, sink)
    runtime = WorkerRuntime(worker=worker, queue=store, health=health)

    try:
        await store.enqueue(
            event_id,
            EventRequest(event_type="delivery.test", payload={}),
        )
        assert await runtime.startup()

        current_delivery = asyncio.create_task(runtime.run_once())
        await sink.started.wait()
        runtime.begin_shutdown()

        assert not health.ready
        assert await runtime.run_once() is None

        sink.release.set()
        assert await current_delivery is WorkerResult.DELIVERED
        assert await runtime.wait_for_drain(timeout_seconds=1)

        status = await redis_client.hgetall(store.status_key(event_id))
        pending = await redis_client.xpending_range(
            store.stream_key,
            "hooklane-workers",
            min="-",
            max="+",
            count=10,
        )
        assert status["status"] == "delivered"
        assert pending == []
        assert sink.event_ids == [event_id]
    finally:
        await redis_client.delete(store.stream_key, store.status_key(event_id))
