from __future__ import annotations

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
async def test_accept_event_atomically_enqueues_and_records_status(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    request = {
        "event_type": "delivery.test",
        "payload": {"message": "accepted"},
    }
    status_key: str | None = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(event_store=store)),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1/events", json=request)

        assert response.status_code == 202
        event_id = UUID(response.json()["event_id"])
        status_key = store.status_key(event_id)
        status_record = await redis_client.hgetall(status_key)
        stream_records = await redis_client.xrange(store.stream_key)

        assert stream_records is not None
        assert len(stream_records) == 1
        stream_id, stream_record = stream_records[0]
        assert isinstance(stream_id, str)
        assert stream_record is not None
        assert stream_record == {
            "event_id": str(event_id),
            "event_type": "delivery.test",
            "payload": '{"message":"accepted"}',
            "accepted_at_ms": stream_record["accepted_at_ms"],
        }
        assert stream_record["accepted_at_ms"].isdigit()
        assert status_record == {
            "event_id": str(event_id),
            "status": "queued",
            "attempt_count": "0",
            "stream_id": stream_id,
        }
    finally:
        keys = [store.stream_key]
        if status_key is not None:
            keys.append(status_key)
        await redis_client.delete(*keys)


@pytest.mark.asyncio
async def test_redis_write_failure_is_not_accepted_or_partially_persisted(
    redis_client: Redis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    payload_marker = uuid4().hex
    await redis_client.set(store.stream_key, "wrong-type")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(event_store=store)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/events",
                json={
                    "event_type": "delivery.test",
                    "payload": {"marker": payload_marker},
                },
            )

        assert response.status_code == 503
        assert response.json() == {"detail": "Event storage unavailable"}
        assert payload_marker not in response.text
        assert payload_marker not in caplog.text
        assert await redis_client.keys(f"{namespace}:event:*") == []
    finally:
        await redis_client.delete(store.stream_key)


@pytest.mark.asyncio
async def test_redis_connection_details_are_not_returned_or_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    password_marker = uuid4().hex
    payload_marker = uuid4().hex
    url = (
        f"unix:///tmp/hooklane-{password_marker}/missing.sock"
        f"?password={password_marker}"
    )
    store = RedisEventStore.from_url(url)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(event_store=store)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/events",
                json={
                    "event_type": "delivery.test",
                    "payload": {"marker": payload_marker},
                },
            )

        assert response.status_code == 503
        assert response.json() == {"detail": "Event storage unavailable"}
        assert password_marker not in response.text
        assert password_marker not in caplog.text
        assert payload_marker not in response.text
        assert payload_marker not in caplog.text
    finally:
        await store.close()
