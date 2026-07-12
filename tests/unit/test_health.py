from __future__ import annotations

import json
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from hooklane.api.app import create_app
from hooklane.domain.events import EventRequest
from hooklane.observability.logging import StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics
from hooklane.queue.events import EventStatusRecord, EventStoreUnavailable
from hooklane.runtime import ServiceHealth
from hooklane.worker.runtime import WorkerRuntime
from hooklane.worker.service import WorkerResult


class AcceptingStore:
    def __init__(self) -> None:
        self.enqueue_calls = 0

    async def enqueue(self, _event_id: UUID, _event: EventRequest) -> None:
        self.enqueue_calls += 1

    async def enqueue_idempotent(
        self,
        event_id: UUID,
        _event: EventRequest,
        _idempotency_key: str,
    ) -> UUID:
        self.enqueue_calls += 1
        return event_id

    async def get_status(self, _event_id: UUID) -> EventStatusRecord | None:
        return None


class ReadinessProbe:
    def __init__(self, available: bool) -> None:
        self.available = available

    async def __call__(self) -> bool:
        return self.available


class QueueProbe:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def ensure_consumer_group(self, _group_name: str) -> None:
        if self.fail:
            raise EventStoreUnavailable


class IdleWorker:
    async def run_once(self) -> WorkerResult:
        return WorkerResult.NO_MESSAGE


@pytest.mark.asyncio
async def test_api_liveness_is_independent_from_readiness() -> None:
    health = ServiceHealth()
    probe = ReadinessProbe(available=False)
    application = create_app(
        event_store=AcceptingStore(),
        health=health,
        readiness_probe=probe,
    )

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503

        health.mark_started(dependency_ready=False)
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.get("/health/live")).status_code == 200

        probe.available = True
        assert (await client.get("/health/ready")).status_code == 200

        probe.available = False
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.get("/health/live")).status_code == 200


@pytest.mark.asyncio
async def test_api_dependency_outage_rejects_without_partial_enqueue() -> None:
    store = AcceptingStore()
    health = ServiceHealth(started=True, dependency_ready=False)
    metrics = HooklaneMetrics("api", include_process_metrics=False)
    lines: list[str] = []
    application = create_app(
        event_store=store,
        health=health,
        metrics=metrics,
        logger=StructuredLogger("api", sink=lines.append),
    )

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/events",
            json={"event_type": "delivery.test", "payload": {}},
        )

    assert response.status_code == 503
    assert "event_id" not in response.json()
    assert store.enqueue_calls == 0
    rendered = metrics.render().decode()
    assert 'outcome="failure",reason_code="storage_unavailable"' in rendered
    records = [json.loads(line) for line in lines]
    assert any(
        record.get("event") == "request_rejected"
        and record.get("reason_code") == "storage_unavailable"
        for record in records
    )


@pytest.mark.asyncio
async def test_worker_is_ready_only_after_queue_startup() -> None:
    ready_health = ServiceHealth()
    ready_runtime = WorkerRuntime(
        worker=IdleWorker(),
        queue=QueueProbe(),
        health=ready_health,
    )
    failed_health = ServiceHealth()
    failed_runtime = WorkerRuntime(
        worker=IdleWorker(),
        queue=QueueProbe(fail=True),
        health=failed_health,
    )

    assert not ready_health.ready
    assert await ready_runtime.startup()
    assert ready_health.ready

    assert not await failed_runtime.startup()
    assert not failed_health.ready
    assert failed_health.live
