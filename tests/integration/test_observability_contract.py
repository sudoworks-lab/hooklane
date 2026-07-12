from __future__ import annotations

from collections.abc import AsyncIterator
import json
import os
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.api.app import create_app as create_api
from hooklane.delivery.sink import MockSinkClient
from hooklane.domain.events import EventRequest
from hooklane.mock_sink.app import MockSinkReceipts, create_app as create_mock_sink
from hooklane.observability.logging import StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics
from hooklane.queue.events import EventStoreUnavailable, RedisEventStore
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
async def test_api_worker_and_sink_logs_correlate_with_bounded_metrics(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    payload_marker = f"payload-{uuid4()}"
    idempotency_marker = f"idempotency-{uuid4()}"
    credential_marker = f"credential-{uuid4()}"
    secret_marker = f"secret-{uuid4()}"
    redis_password_marker = f"redis-password-{uuid4()}"
    redis_url_marker = f"redis-url-{uuid4()}"
    cookie_marker = f"cookie-{uuid4()}"
    personal_marker = f"personal-{uuid4()}"
    lines: list[str] = []
    api_metrics = HooklaneMetrics("api", include_process_metrics=False)
    worker_metrics = HooklaneMetrics("worker", include_process_metrics=False)
    sink_metrics = HooklaneMetrics("mock_sink", include_process_metrics=False)
    api_store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=api_metrics,
        logger=StructuredLogger("api", sink=lines.append),
    )
    worker_store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=worker_metrics,
        logger=StructuredLogger("worker", sink=lines.append),
    )
    receipts = MockSinkReceipts()
    sink = MockSinkClient(
        transport=ASGITransport(
            app=create_mock_sink(
                receipts=receipts,
                metrics=sink_metrics,
                logger=StructuredLogger("mock_sink", sink=lines.append),
            )
        )
    )
    worker = EventWorker(
        worker_store,
        sink,
        metrics=worker_metrics,
        logger=StructuredLogger("worker", sink=lines.append),
    )
    event_id: UUID | None = None

    try:
        application = create_api(
            event_store=api_store,
            metrics=api_metrics,
            logger=StructuredLogger("api", sink=lines.append),
        )
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/events",
                json={
                    "event_type": "delivery.test",
                    "payload": {
                        "payload_marker": payload_marker,
                        "credential_marker": credential_marker,
                        "secret_marker": secret_marker,
                        "redis_password_marker": redis_password_marker,
                        "redis_url_marker": redis_url_marker,
                        "personal_marker": personal_marker,
                    },
                },
                headers={
                    "Idempotency-Key": idempotency_marker,
                    "Cookie": cookie_marker,
                },
            )
        assert response.status_code == 202
        event_id = UUID(response.json()["event_id"])
        assert await api_store.refresh_queue_metrics()
        assert 'hooklane_queue_depth{service="api"} 1.0' in api_metrics.render().decode()

        assert await worker.run_once() is WorkerResult.DELIVERED
        assert await api_store.refresh_queue_metrics()
        api_text = api_metrics.render().decode()
        worker_text = worker_metrics.render().decode()
        sink_text = sink_metrics.render().decode()
        records = [json.loads(line) for line in lines]

        assert 'hooklane_queue_depth{service="api"} 0.0' in api_text
        assert 'hooklane_pending_messages{service="api"} 0.0' in api_text
        assert 'hooklane_delivery_attempts_total{service="worker"} 1.0' in worker_text
        assert 'outcome="success",reason_code="none",service="worker"' in worker_text
        assert 'hooklane_delivery_completion_total{outcome="within_60_seconds",service="worker"} 1.0' in worker_text
        assert receipts.event_ids == frozenset({event_id})
        assert any(
            record["service"] == "api"
            and record["event"] == "event_accepted"
            and record["event_id"] == str(event_id)
            and "request_id" in record
            for record in records
        )
        assert any(
            record["service"] == "worker" and record.get("event_id") == str(event_id)
            for record in records
        )
        assert any(
            record["service"] == "mock_sink" and record.get("event_id") == str(event_id)
            for record in records
        )
        public_observability = "\n".join(lines) + api_text + worker_text + sink_text
        for marker in (
            payload_marker,
            idempotency_marker,
            credential_marker,
            secret_marker,
            redis_password_marker,
            redis_url_marker,
            cookie_marker,
            personal_marker,
        ):
            assert marker not in public_observability
        for forbidden_label in ("event_id=", "request_id=", "idempotency_key="):
            assert forbidden_label not in api_text + worker_text + sink_text
    finally:
        await sink.close()
        keys = [
            api_store.stream_key,
            api_store.retry_schedule_key,
            api_store.dead_letter_key,
        ]
        if event_id is not None:
            keys.extend(
                [
                    api_store.status_key(event_id),
                    api_store.idempotency_key(idempotency_marker),
                ]
            )
        await redis_client.delete(*keys)


@pytest.mark.asyncio
async def test_redis_failure_uses_reason_code_without_error_or_payload(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    payload_marker = f"payload-{uuid4()}"
    lines: list[str] = []
    metrics = HooklaneMetrics("api", include_process_metrics=False)
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("api", sink=lines.append),
    )
    await redis_client.set(store.stream_key, "incompatible")

    try:
        with pytest.raises(EventStoreUnavailable):
            await store.enqueue(
                uuid4(),
                EventRequest(
                    event_type="delivery.test",
                    payload={"marker": payload_marker},
                ),
            )
        rendered = metrics.render().decode()
        assert 'operation="enqueue"' in rendered
        assert payload_marker not in rendered
        assert payload_marker not in "\n".join(lines)
        record = json.loads(lines[-1])
        assert record["event"] == "redis_operation_failed"
        assert record["reason_code"] == "redis_error"
        assert "exception" not in record
    finally:
        await redis_client.delete(store.stream_key)
