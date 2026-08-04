from __future__ import annotations

from collections.abc import AsyncIterator
import json
import os
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport
from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.delivery.sink import MockSinkClient
from hooklane.domain.events import EventRequest
from hooklane.mock_sink.app import MockSinkReceipts, create_app
from hooklane.observability.logging import StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics
from hooklane.queue.events import EventStoreUnavailable, RedisEventStore
from hooklane.worker.service import EventWorker, WorkerResult


GROUP_NAME = "hooklane-workers"


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


def _stream_fields(
    event_id: str,
    *,
    event_type: str = "delivery.test",
    payload: str = "{}",
    accepted_at_ms: str = "1000",
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "payload": payload,
        "accepted_at_ms": accepted_at_ms,
    }


def _quarantine_key(store: RedisEventStore) -> str:
    return f"{store.stream_key.removesuffix(':events')}:quarantine"


async def _delete_namespace(
    redis_client: Redis,
    store: RedisEventStore,
    *event_ids: UUID | None,
) -> None:
    await redis_client.delete(
        store.stream_key,
        store.retry_schedule_key,
        store.dead_letter_key,
        _quarantine_key(store),
        *(store.status_key(event_id) for event_id in event_ids if event_id is not None),
    )


async def _run_bounded(worker: EventWorker, attempts: int = 3) -> list[str]:
    outcomes: list[str] = []
    for _ in range(attempts):
        try:
            result = await worker.run_once()
        except EventStoreUnavailable:
            outcomes.append("event_store_unavailable")
        else:
            outcomes.append(result.value)
    return outcomes


async def _pending(redis_client: Redis, store: RedisEventStore) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        await redis_client.xpending_range(
            store.stream_key,
            GROUP_NAME,
            min="-",
            max="+",
            count=20,
        ),
    )


async def _normal_sink() -> tuple[MockSinkClient, MockSinkReceipts]:
    receipts = MockSinkReceipts()
    sink = MockSinkClient(
        transport=ASGITransport(
            app=create_app(
                receipts=receipts,
                logger=StructuredLogger("mock_sink", sink=lambda _line: None),
            )
        )
    )
    return sink, receipts


async def _enqueue_normal(store: RedisEventStore) -> UUID:
    event_id = uuid4()
    await store.enqueue(
        event_id,
        EventRequest(event_type="delivery.test", payload={}),
    )
    return event_id


async def _set_retry_record(
    redis_client: Redis,
    store: RedisEventStore,
    event_id: UUID,
    stream_id: str,
) -> None:
    await redis_client.hset(
        store.status_key(event_id),
        mapping={
            "event_id": str(event_id),
            "status": "retry_scheduled",
            "attempt_count": "1",
            "stream_id": stream_id,
        },
    )
    await redis_client.zadd(store.retry_schedule_key, {str(event_id): 0})


async def _assert_h01_outcome(
    redis_client: Redis,
    store: RedisEventStore,
    worker: EventWorker,
    normal_event_id: UUID,
    poison_event_id: UUID,
    *,
    status_expected: str | None,
    logger_lines: list[str],
    metrics: HooklaneMetrics,
    receipts: MockSinkReceipts,
    expected_quarantine_count: int = 0,
    expected_metric_count: int = 1,
    expected_last_error_class: str | None = "invariant_violation",
) -> None:
    outcomes = await _run_bounded(worker)
    status = await redis_client.hgetall(store.status_key(poison_event_id))
    pending = await _pending(redis_client, store)
    retry_count = await redis_client.zcard(store.retry_schedule_key)
    quarantine = cast(
        list[tuple[str, dict[str, str]]],
        await redis_client.xrange(_quarantine_key(store)),
    )
    rendered_metrics = metrics.render().decode()
    print(
        json.dumps(
            {
                "hypothesis": "H-01",
                "stream_type": await redis_client.type(store.stream_key),
                "status_type": await redis_client.type(store.status_key(poison_event_id)),
                "retry_zset_type": await redis_client.type(store.retry_schedule_key),
                "pending_count": len(pending),
                "retry_zset_count": retry_count,
                "quarantine_count": len(quarantine),
                "status": status.get("status", "missing"),
                "last_error_class": status.get("last_error_class", "missing"),
                "normal_event_delivered": normal_event_id in receipts.event_ids,
                "worker_outcomes": outcomes,
                "quarantine_metric_present": "hooklane_queue_quarantined_total" in rendered_metrics,
                "raw_log_line_count": len(logger_lines),
            },
            sort_keys=True,
        )
    )
    assert outcomes[0] == WorkerResult.DELIVERED.value
    assert retry_count == 0
    assert (
        'hooklane_queue_quarantined_total{reason_code="invariant_violation",service="worker"} '
        f"{expected_metric_count}.0"
        in rendered_metrics
    )
    assert len(quarantine) == expected_quarantine_count
    if expected_quarantine_count:
        assert all(
            fields["reason_code"] == "invariant_violation"
            for _stream_id, fields in quarantine
        )
    if status_expected is None:
        assert not status
    else:
        assert status["status"] == status_expected
        if expected_last_error_class is None:
            assert "last_error_class" not in status
        else:
            assert status["last_error_class"] == expected_last_error_class


@pytest.mark.asyncio
async def test_h01_missing_status_does_not_block_following_event(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    poison_event_id = uuid4()
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    sink, receipts = await _normal_sink()
    normal_event_id: UUID | None = None
    try:
        await redis_client.zadd(store.retry_schedule_key, {str(poison_event_id): 0})
        normal_event_id = await _enqueue_normal(store)
        assert normal_event_id is not None
        await _assert_h01_outcome(
            redis_client,
            store,
            EventWorker(store, sink),
            normal_event_id,
            poison_event_id,
            status_expected=None,
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
        )
        assert receipts.event_ids == frozenset({normal_event_id})
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, poison_event_id, normal_event_id)


@pytest.mark.asyncio
async def test_h01_retry_status_without_pending_does_not_block_following_event(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    poison_event_id = uuid4()
    sink, receipts = await _normal_sink()
    normal_event_id: UUID | None = None
    try:
        poison_stream_id = await redis_client.xadd(
            store.stream_key,
            cast(Any, _stream_fields(str(poison_event_id))),
        )
        poison_stream_id = cast(str, poison_stream_id)
        await redis_client.xgroup_create(
            store.stream_key,
            GROUP_NAME,
            id=poison_stream_id,
        )
        await _set_retry_record(redis_client, store, poison_event_id, poison_stream_id)
        normal_event_id = await _enqueue_normal(store)
        assert normal_event_id is not None
        worker = EventWorker(store, sink)
        await _assert_h01_outcome(
            redis_client,
            store,
            worker,
            normal_event_id,
            poison_event_id,
            status_expected="dead_letter",
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
        )
        assert receipts.event_ids == frozenset({normal_event_id})
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, poison_event_id, normal_event_id)


@pytest.mark.asyncio
async def test_h01_missing_retry_stream_message_does_not_block_following_event(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    poison_event_id = uuid4()
    sink, receipts = await _normal_sink()
    normal_event_id: UUID | None = None
    try:
        await _set_retry_record(
            redis_client,
            store,
            poison_event_id,
            "9999999999999-0",
        )
        normal_event_id = await _enqueue_normal(store)
        assert normal_event_id is not None
        await _assert_h01_outcome(
            redis_client,
            store,
            EventWorker(store, sink),
            normal_event_id,
            poison_event_id,
            status_expected="dead_letter",
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
        )
        assert receipts.event_ids == frozenset({normal_event_id})
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, poison_event_id, normal_event_id)


@pytest.mark.asyncio
async def test_h01_mismatched_retry_stream_message_does_not_block_following_event(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    poison_event_id = uuid4()
    mismatched_event_id = uuid4()
    sink, receipts = await _normal_sink()
    normal_event_id: UUID | None = None
    try:
        await store.ensure_consumer_group(GROUP_NAME)
        poison_stream_id = await redis_client.xadd(
            store.stream_key,
            cast(Any, _stream_fields(str(mismatched_event_id))),
        )
        poison_stream_id = cast(str, poison_stream_id)
        records = await redis_client.xreadgroup(
            GROUP_NAME,
            "poison-owner",
            {store.stream_key: ">"},
            count=1,
        )
        assert records
        await _set_retry_record(redis_client, store, poison_event_id, poison_stream_id)
        normal_event_id = await _enqueue_normal(store)
        assert normal_event_id is not None
        await _assert_h01_outcome(
            redis_client,
            store,
            EventWorker(store, sink, pending_idle_ms=0),
            normal_event_id,
            poison_event_id,
            status_expected="dead_letter",
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
            expected_quarantine_count=1,
            expected_metric_count=2,
        )
        assert receipts.event_ids == frozenset({normal_event_id})
    finally:
        await sink.close()
        await _delete_namespace(
            redis_client,
            store,
            poison_event_id,
            mismatched_event_id,
            normal_event_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_status", "existing_reason"),
    [
        ("delivered", "none"),
        ("dead_letter", "http_4xx"),
        ("queued", "none"),
        ("delivering", "http_5xx"),
        ("unknown", "http_5xx"),
    ],
    ids=["delivered", "dead_letter", "queued", "delivering", "unknown"],
)
async def test_h01_stale_retry_member_preserves_non_retry_status(
    redis_client: Redis,
    current_status: str,
    existing_reason: str,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    poison_event_id = uuid4()
    sink, receipts = await _normal_sink()
    normal_event_id: UUID | None = None
    try:
        await redis_client.hset(
            store.status_key(poison_event_id),
            mapping={
                "event_id": str(poison_event_id),
                "status": current_status,
                "attempt_count": "1",
                "stream_id": "9999999999999-0",
                "last_error_class": existing_reason,
            },
        )
        await redis_client.zadd(
            store.retry_schedule_key,
            {str(poison_event_id): 0},
        )
        normal_event_id = await _enqueue_normal(store)
        await _assert_h01_outcome(
            redis_client,
            store,
            EventWorker(store, sink),
            normal_event_id,
            poison_event_id,
            status_expected=current_status,
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
            expected_last_error_class=existing_reason,
        )
        status = await redis_client.hgetall(store.status_key(poison_event_id))
        assert status["event_id"] == str(poison_event_id)
        assert status["last_error_class"] == existing_reason
        assert receipts.event_ids == frozenset({normal_event_id})
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, poison_event_id, normal_event_id)


@pytest.mark.asyncio
async def test_h01_incomplete_retry_scheduled_does_not_invent_fields(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    poison_event_id = uuid4()
    sink, receipts = await _normal_sink()
    normal_event_id: UUID | None = None
    try:
        await redis_client.hset(
            store.status_key(poison_event_id),
            mapping={
                "event_id": str(poison_event_id),
                "status": "retry_scheduled",
            },
        )
        await redis_client.zadd(
            store.retry_schedule_key,
            {str(poison_event_id): 0},
        )
        normal_event_id = await _enqueue_normal(store)
        await _assert_h01_outcome(
            redis_client,
            store,
            EventWorker(store, sink),
            normal_event_id,
            poison_event_id,
            status_expected="retry_scheduled",
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
            expected_last_error_class=None,
        )
        status = await redis_client.hgetall(store.status_key(poison_event_id))
        assert set(status) == {"event_id", "status"}
        assert receipts.event_ids == frozenset({normal_event_id})
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, poison_event_id, normal_event_id)


@pytest.mark.asyncio
async def test_redis_failure_during_retry_release_remains_fail_closed(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    try:
        await redis_client.set(store.stream_key, "incompatible")
        await redis_client.zadd(store.retry_schedule_key, {str(uuid4()): 0})
        with pytest.raises(EventStoreUnavailable):
            await store.release_due_retry(GROUP_NAME, 1_000)
        assert await redis_client.zcard(store.retry_schedule_key) == 1
        assert 'operation="release_retry"' in metrics.render().decode()
        assert json.loads(logger_lines[-1])["reason_code"] == "redis_error"
    finally:
        await _delete_namespace(redis_client, store)


async def _seed_malformed_message(
    redis_client: Redis,
    store: RedisEventStore,
    *,
    event_id_field: str,
    payload: str,
    accepted_at_ms: str,
    status_event_id: UUID | None,
    status: str = "queued",
    pending_consumer: str | None = None,
    event_type: str = "delivery.test",
    omit_fields: set[str] | None = None,
    last_error_class: str | None = None,
) -> tuple[str, UUID | None]:
    await store.ensure_consumer_group(GROUP_NAME)
    fields = _stream_fields(
        event_id_field,
        event_type=event_type,
        payload=payload,
        accepted_at_ms=accepted_at_ms,
    )
    for field in omit_fields or set():
        fields.pop(field, None)
    stream_id = await redis_client.xadd(
        store.stream_key,
        cast(Any, fields),
    )
    stream_id = cast(str, stream_id)
    if status_event_id is not None:
        status_mapping: dict[str, Any] = {
            "event_id": str(status_event_id),
            "status": status,
            "attempt_count": "0",
            "stream_id": stream_id,
        }
        if last_error_class is not None:
            status_mapping["last_error_class"] = last_error_class
        await redis_client.hset(
            store.status_key(status_event_id),
            mapping=cast(Any, status_mapping),
        )
    if pending_consumer is not None:
        records = await redis_client.xreadgroup(
            GROUP_NAME,
            pending_consumer,
            {store.stream_key: ">"},
            count=1,
        )
        assert records
    return stream_id, status_event_id


async def _assert_h02_quarantined(
    redis_client: Redis,
    store: RedisEventStore,
    worker: EventWorker,
    *,
    status_event_id: UUID | None,
    normal_event_id: UUID | None,
    expected_event_id_metadata: bool,
    logger_lines: list[str],
    metrics: HooklaneMetrics,
    receipts: MockSinkReceipts | None = None,
    expected_reason_code: str = "invalid_message",
    expected_status: str | None = "dead_letter",
    expected_last_error_class: str | None = "invalid_message",
) -> None:
    outcomes = await _run_bounded(worker)
    pending = await _pending(redis_client, store)
    status = (
        await redis_client.hgetall(store.status_key(status_event_id))
        if status_event_id is not None
        else {}
    )
    quarantine = cast(
        list[tuple[str, dict[str, str]]],
        await redis_client.xrange(_quarantine_key(store)),
    )
    quarantine_fields = set(quarantine[0][1]) if quarantine else set()
    rendered_metrics = metrics.render().decode()
    print(
        json.dumps(
            {
                "hypothesis": "H-02",
                "stream_type": await redis_client.type(store.stream_key),
                "quarantine_stream_type": await redis_client.type(_quarantine_key(store)),
                "pending_count": len(pending),
                "status": status.get("status", "missing"),
                "last_error_class": status.get("last_error_class", "missing"),
                "quarantine_count": len(quarantine),
                "quarantine_has_raw_payload": any(
                    "payload" in fields for _stream_id, fields in quarantine
                ),
                "quarantine_has_event_id_metadata": "event_id" in quarantine_fields,
                "normal_event_delivered": (
                    normal_event_id is not None
                    and receipts is not None
                    and normal_event_id in receipts.event_ids
                ),
                "worker_outcomes": outcomes,
                "quarantine_metric_present": "hooklane_queue_quarantined_total" in rendered_metrics,
                "raw_log_line_count": len(logger_lines),
            },
            sort_keys=True,
        )
    )
    assert outcomes[0] == WorkerResult.NO_MESSAGE.value
    assert pending == []
    assert len(quarantine) == 1
    assert quarantine[0][1]["reason_code"] == expected_reason_code
    assert "payload" not in quarantine[0][1]
    expected_quarantine_fields = {"source_stream_id", "reason_code"}
    if expected_event_id_metadata:
        expected_quarantine_fields.add("event_id")
    assert set(quarantine[0][1]) == expected_quarantine_fields
    assert (
        f'hooklane_queue_quarantined_total{{reason_code="{expected_reason_code}",service="worker"}} 1.0'
        in rendered_metrics
    )
    assert ("event_id" in quarantine_fields) is expected_event_id_metadata
    if status_event_id is None:
        assert not status
    elif expected_status is None:
        assert not status
    else:
        assert status["status"] == expected_status
        if expected_last_error_class is None:
            assert "last_error_class" not in status
        else:
            assert status["last_error_class"] == expected_last_error_class


@pytest.mark.asyncio
async def test_h02_invalid_json_is_acked_and_status_is_terminal(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    event_id = uuid4()
    sink, receipts = await _normal_sink()
    try:
        marker = uuid4().hex
        malformed_payload = "{" + marker
        await _seed_malformed_message(
            redis_client,
            store,
            event_id_field=str(event_id),
            payload=malformed_payload,
            accepted_at_ms="1000",
            status_event_id=event_id,
        )
        await _assert_h02_quarantined(
            redis_client,
            store,
            EventWorker(store, sink, pending_idle_ms=0),
            status_event_id=event_id,
            normal_event_id=None,
            expected_event_id_metadata=True,
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
        )
        assert marker not in "\n".join(logger_lines)
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, event_id)


@pytest.mark.asyncio
async def test_h02_invalid_uuid_is_acked_without_creating_status(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    sink, receipts = await _normal_sink()
    try:
        malformed_event_id = str(uuid4())[:-1]
        await _seed_malformed_message(
            redis_client,
            store,
            event_id_field=malformed_event_id,
            payload="{}",
            accepted_at_ms="1000",
            status_event_id=None,
        )
        await _assert_h02_quarantined(
            redis_client,
            store,
            EventWorker(store, sink, pending_idle_ms=0),
            status_event_id=None,
            normal_event_id=None,
            expected_event_id_metadata=False,
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
        )
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store)


@pytest.mark.asyncio
async def test_h02_invalid_accepted_at_is_acked_and_status_is_terminal(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    event_id = uuid4()
    sink, receipts = await _normal_sink()
    try:
        await _seed_malformed_message(
            redis_client,
            store,
            event_id_field=str(event_id),
            payload="{}",
            accepted_at_ms=str(uuid4()),
            status_event_id=event_id,
        )
        await _assert_h02_quarantined(
            redis_client,
            store,
            EventWorker(store, sink, pending_idle_ms=0),
            status_event_id=event_id,
            normal_event_id=None,
            expected_event_id_metadata=True,
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
        )
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "accepted_at_ms", "omit_fields"),
    [
        pytest.param(
            "delivery.test",
            "1000",
            {"event_type"},
            id="missing_event_type",
        ),
        pytest.param("", "1000", set(), id="blank_event_type"),
        pytest.param("   ", "1000", set(), id="whitespace_event_type"),
        pytest.param("delivery.test", "1.5", set(), id="fractional_accepted_at"),
        pytest.param("delivery.test", "1e3", set(), id="scientific_accepted_at"),
    ],
)
async def test_h02_python_parser_rejects_malformed_fields(
    redis_client: Redis,
    event_type: str,
    accepted_at_ms: str,
    omit_fields: set[str],
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    event_id = uuid4()
    sink, receipts = await _normal_sink()
    try:
        await _seed_malformed_message(
            redis_client,
            store,
            event_id_field=str(event_id),
            payload="{}",
            accepted_at_ms=accepted_at_ms,
            status_event_id=event_id,
            event_type=event_type,
            omit_fields=omit_fields,
        )
        await _assert_h02_quarantined(
            redis_client,
            store,
            EventWorker(store, sink, pending_idle_ms=0),
            status_event_id=event_id,
            normal_event_id=None,
            expected_event_id_metadata=True,
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
        )
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, event_id)


@pytest.mark.asyncio
async def test_h02_retry_scheduled_pending_is_left_for_retry_release(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    event_id = uuid4()
    sink, _receipts = await _normal_sink()
    try:
        await store.ensure_consumer_group(GROUP_NAME)
        stream_id = await redis_client.xadd(
            store.stream_key,
            cast(Any, _stream_fields(str(event_id))),
        )
        stream_id = cast(str, stream_id)
        records = await redis_client.xreadgroup(
            GROUP_NAME,
            "retry-owner",
            {store.stream_key: ">"},
            count=1,
        )
        assert records
        await redis_client.hset(
            store.status_key(event_id),
            mapping={
                "event_id": str(event_id),
                "status": "retry_scheduled",
                "attempt_count": "1",
                "stream_id": stream_id,
            },
        )
        await redis_client.zadd(store.retry_schedule_key, {str(event_id): 2_000})
        worker = EventWorker(
            store,
            sink,
            clock=lambda: 1.0,
            pending_idle_ms=0,
        )
        assert await worker.run_once() is WorkerResult.NO_MESSAGE
        pending = await _pending(redis_client, store)
        assert len(pending) == 1
        assert await redis_client.zcard(store.retry_schedule_key) == 1
        assert await redis_client.xlen(_quarantine_key(store)) == 0
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "existing_reason"),
    [
        pytest.param("delivered", "none", id="delivered"),
        pytest.param("dead_letter", "http_4xx", id="dead_letter"),
    ],
)
async def test_h02_malformed_message_preserves_terminal_status(
    redis_client: Redis,
    status: str,
    existing_reason: str,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    event_id = uuid4()
    marker = uuid4().hex
    sink, receipts = await _normal_sink()
    try:
        await _seed_malformed_message(
            redis_client,
            store,
            event_id_field=str(event_id),
            payload="{" + marker,
            accepted_at_ms="1000",
            status_event_id=event_id,
            status=status,
            last_error_class=existing_reason,
        )
        await _assert_h02_quarantined(
            redis_client,
            store,
            EventWorker(store, sink, pending_idle_ms=0),
            status_event_id=event_id,
            normal_event_id=None,
            expected_event_id_metadata=True,
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
            expected_status=status,
            expected_last_error_class=existing_reason,
        )
        assert marker not in "\n".join(logger_lines)
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, event_id)


@pytest.mark.asyncio
async def test_h02_valid_pending_without_status_is_quarantined_without_fake_status(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    event_id = uuid4()
    sink, receipts = await _normal_sink()
    try:
        stream_id, _status_event_id = await _seed_malformed_message(
            redis_client,
            store,
            event_id_field=str(event_id),
            payload="{}",
            accepted_at_ms="1000",
            status_event_id=None,
            pending_consumer="worker-1",
        )
        await _assert_h02_quarantined(
            redis_client,
            store,
            EventWorker(
                store,
                sink,
                consumer_name="worker-2",
                pending_idle_ms=0,
            ),
            status_event_id=None,
            normal_event_id=None,
            expected_event_id_metadata=True,
            logger_lines=logger_lines,
            metrics=metrics,
            receipts=receipts,
            expected_reason_code="invariant_violation",
            expected_status=None,
        )
        assert not await redis_client.exists(store.status_key(event_id))
        assert stream_id
        assert receipts.event_ids == frozenset()
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, event_id)


@pytest.mark.asyncio
async def test_h02_valid_queued_pending_is_claimed_and_delivered(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    store = RedisEventStore(redis_client, namespace=namespace)
    event_id = uuid4()
    sink, receipts = await _normal_sink()
    try:
        stream_id, _status_event_id = await _seed_malformed_message(
            redis_client,
            store,
            event_id_field=str(event_id),
            payload="{}",
            accepted_at_ms="1000",
            status_event_id=event_id,
            pending_consumer="worker-1",
        )
        worker = EventWorker(
            store,
            sink,
            consumer_name="worker-2",
            pending_idle_ms=0,
        )
        assert await worker.run_once() is WorkerResult.DELIVERED
        status = await redis_client.hgetall(store.status_key(event_id))
        assert status["status"] == "delivered"
        assert status["attempt_count"] == "1"
        assert status["stream_id"] == stream_id
        assert await _pending(redis_client, store) == []
        assert await redis_client.xlen(_quarantine_key(store)) == 0
        assert receipts.event_ids == frozenset({event_id})
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, event_id)


@pytest.mark.asyncio
async def test_h02_malformed_entry_is_recovered_and_following_event_is_delivered(
    redis_client: Redis,
) -> None:
    namespace = f"hooklane:test:{uuid4().hex}"
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    logger_lines: list[str] = []
    store = RedisEventStore(
        redis_client,
        namespace=namespace,
        metrics=metrics,
        logger=StructuredLogger("worker", sink=logger_lines.append),
    )
    malformed_event_id = uuid4()
    sink, receipts = await _normal_sink()
    normal_event_id: UUID | None = None
    try:
        await _seed_malformed_message(
            redis_client,
            store,
            event_id_field=str(malformed_event_id),
            payload="{" + uuid4().hex,
            accepted_at_ms="1000",
            status_event_id=malformed_event_id,
            status="delivering",
            pending_consumer="worker-1",
        )
        normal_event_id = await _enqueue_normal(store)
        assert normal_event_id is not None
        recovery_worker = EventWorker(
            store,
            sink,
            consumer_name="worker-2",
            pending_idle_ms=0,
        )
        outcomes = await _run_bounded(recovery_worker)
        pending = await _pending(redis_client, store)
        status = await redis_client.hgetall(store.status_key(malformed_event_id))
        quarantine = cast(
            list[tuple[str, dict[str, str]]],
            await redis_client.xrange(_quarantine_key(store)),
        )
        print(
            json.dumps(
                {
                    "hypothesis": "H-02",
                    "case": "pending_recovery",
                    "pending_count": len(pending),
                    "status": status.get("status", "missing"),
                    "quarantine_count": len(quarantine),
                    "normal_event_delivered": normal_event_id in receipts.event_ids,
                    "worker_outcomes": outcomes,
                },
                sort_keys=True,
            )
        )
        assert outcomes[0] == WorkerResult.DELIVERED.value
        assert pending == []
        assert status["status"] == "dead_letter"
        assert status["last_error_class"] == "invalid_message"
        assert len(quarantine) == 1
        assert normal_event_id in receipts.event_ids
    finally:
        await sink.close()
        await _delete_namespace(redis_client, store, malformed_event_id, normal_event_id)
