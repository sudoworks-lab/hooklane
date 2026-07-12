from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from hooklane.api.app import create_app
from hooklane.delivery.dead_letter import DeadLetterPolicy
from hooklane.delivery.retry import DeliveryErrorClass, RetryPolicy
from hooklane.delivery.sink import DeliveryFailed
from hooklane.domain.events import EventRequest
from hooklane.observability.logging import StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics, METRIC_LABELS
from hooklane.queue.events import EventStatusRecord, QueuedEvent
from hooklane.worker.service import EventWorker, WorkerResult


FORBIDDEN_LABELS = {
    "event_id",
    "request_id",
    "idempotency_key",
    "url",
    "payload_type",
    "exception_message",
    "user_input",
}


class MetricsEventStore:
    async def enqueue(self, _event_id: UUID, _event: EventRequest) -> None:
        pass

    async def enqueue_idempotent(
        self,
        event_id: UUID,
        _event: EventRequest,
        _idempotency_key: str,
    ) -> UUID:
        return event_id

    async def get_status(self, _event_id: UUID) -> EventStatusRecord | None:
        return None

    async def refresh_queue_metrics(self, group_name: str = "hooklane-workers") -> bool:
        assert group_name == "hooklane-workers"
        return True


class MetricsWorkerQueue:
    def __init__(self, queued_event: QueuedEvent, metrics: HooklaneMetrics) -> None:
        self._queued_event = queued_event
        self._metrics = metrics
        self._read = False

    async def ensure_consumer_group(self, _group_name: str) -> None:
        pass

    async def release_due_retry(self, _group_name: str, _now_ms: int) -> bool:
        return False

    async def claim_stale_pending(
        self,
        _group_name: str,
        _consumer_name: str,
        _min_idle_ms: int,
    ) -> QueuedEvent | None:
        return None

    async def read_next(
        self,
        _group_name: str,
        _consumer_name: str,
    ) -> QueuedEvent | None:
        if self._read:
            return None
        self._read = True
        return self._queued_event

    async def mark_delivery_started(self, _queued_event: QueuedEvent) -> int:
        return 1

    async def mark_delivered(self, _queued_event: QueuedEvent, _group_name: str) -> None:
        pass

    async def schedule_retry(
        self,
        _queued_event: QueuedEvent,
        _due_at_ms: int,
        _error_class: str,
    ) -> None:
        pass

    async def move_to_dead_letter(
        self,
        _queued_event: QueuedEvent,
        _group_name: str,
        _error_class: str,
    ) -> None:
        pass

    async def refresh_queue_metrics(self, group_name: str = "hooklane-workers") -> bool:
        assert group_name == "hooklane-workers"
        self._metrics.set_queue_state(depth=1, oldest_age_seconds=2, pending=1)
        return True


class FailingDeliverySink:
    def __init__(self, error_class: DeliveryErrorClass) -> None:
        self._error_class = error_class

    async def deliver(self, _queued_event: QueuedEvent) -> None:
        raise DeliveryFailed(self._error_class)


def test_metric_names_updates_and_cardinality_contract() -> None:
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    metrics.record_http("DELETE", "/raw/user/input", 503, 0.25)
    metrics.record_enqueue("success")
    metrics.record_enqueue("failure", "storage_unavailable")
    metrics.set_queue_state(depth=3, oldest_age_seconds=12.5, pending=2)
    metrics.start_delivery()
    metrics.finish_delivery(
        outcome="retry_scheduled",
        reason_code="http_5xx",
        duration_seconds=0.4,
    )
    metrics.record_retry("http_5xx")
    metrics.record_dead_letter("http_4xx")
    metrics.record_redis_failure("enqueue")
    metrics.set_ready(True)
    rendered = metrics.render().decode()

    required_names = {
        "hooklane_http_requests_total",
        "hooklane_http_request_duration_seconds",
        "hooklane_enqueue_total",
        "hooklane_queue_depth",
        "hooklane_oldest_queued_event_age_seconds",
        "hooklane_delivery_attempts_total",
        "hooklane_delivery_outcomes_total",
        "hooklane_delivery_duration_seconds",
        "hooklane_retry_scheduled_total",
        "hooklane_dead_letter_total",
        "hooklane_worker_in_flight",
        "hooklane_pending_messages",
        "hooklane_redis_operation_failures_total",
        "hooklane_service_ready",
    }
    assert required_names <= set(METRIC_LABELS)
    assert all(FORBIDDEN_LABELS.isdisjoint(labels) for labels in METRIC_LABELS.values())
    assert 'method="OTHER"' in rendered
    assert 'route="unmatched"' in rendered
    assert "/raw/user/input" not in rendered
    assert 'hooklane_queue_depth{service="worker"} 3.0' in rendered
    assert 'hooklane_pending_messages{service="worker"} 2.0' in rendered
    assert 'hooklane_retry_scheduled_total{reason_code="http_5xx",service="worker"} 1.0' in rendered
    assert 'hooklane_dead_letter_total{reason_code="http_4xx",service="worker"} 1.0' in rendered
    assert 'operation="enqueue"' in rendered
    assert 'hooklane_worker_in_flight{service="worker"} 0.0' in rendered

    with pytest.raises(ValueError, match="reason code"):
        metrics.record_retry(f"unbounded-{uuid4()}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_class", "expected_result", "expected_metric"),
    [
        (
            DeliveryErrorClass.HTTP_5XX,
            WorkerResult.RETRY_SCHEDULED,
            "hooklane_retry_scheduled_total",
        ),
        (
            DeliveryErrorClass.HTTP_4XX,
            WorkerResult.DEAD_LETTERED,
            "hooklane_dead_letter_total",
        ),
    ],
)
async def test_worker_updates_retry_dead_letter_and_pending_metrics(
    error_class: DeliveryErrorClass,
    expected_result: WorkerResult,
    expected_metric: str,
) -> None:
    metrics = HooklaneMetrics("worker", include_process_metrics=False)
    queued_event = QueuedEvent(
        stream_id="1-0",
        event_id=uuid4(),
        event=EventRequest(event_type="delivery.test", payload={}),
    )
    queue = MetricsWorkerQueue(queued_event, metrics)
    worker = EventWorker(
        queue,
        FailingDeliverySink(error_class),
        retry_policy=RetryPolicy(jitter_ratio=0),
        dead_letter_policy=DeadLetterPolicy(),
        metrics=metrics,
    )

    assert await worker.run_once() is expected_result
    rendered = metrics.render().decode()
    assert expected_metric in rendered
    assert 'hooklane_pending_messages{service="worker"} 1.0' in rendered
    assert 'hooklane_worker_in_flight{service="worker"} 0.0' in rendered


@pytest.mark.asyncio
async def test_api_request_metrics_and_dedicated_metrics_endpoint() -> None:
    payload_marker = f"payload-{uuid4()}"
    idempotency_marker = f"idempotency-{uuid4()}"
    lines: list[str] = []
    metrics = HooklaneMetrics("api")
    application = create_app(
        event_store=MetricsEventStore(),
        metrics=metrics,
        logger=StructuredLogger("api", sink=lines.append),
    )

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        accepted = await client.post(
            "/v1/events",
            json={"event_type": "delivery.test", "payload": {"marker": payload_marker}},
            headers={"Idempotency-Key": idempotency_marker},
        )
        health = await client.get("/health/live")
        response = await client.get("/metrics")

    assert accepted.status_code == 202
    assert UUID(accepted.headers["X-Request-ID"])
    assert health.json() == {"status": "live"}
    assert "hooklane_http_requests_total" not in health.text
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'route="/v1/events"' in response.text
    assert "hooklane_process_cpu_seconds" in response.text
    assert payload_marker not in response.text
    assert idempotency_marker not in response.text
    assert payload_marker not in "\n".join(lines)
    assert idempotency_marker not in "\n".join(lines)
    assert "event_id=" not in response.text
    assert "request_id=" not in response.text
