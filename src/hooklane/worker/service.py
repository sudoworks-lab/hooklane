"""Redis Streams worker for at-least-once mock-sink delivery."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from random import Random
import time
from typing import Protocol, runtime_checkable

from hooklane.delivery.dead_letter import DeadLetterPolicy
from hooklane.delivery.sink import DeliveryFailed
from hooklane.delivery.retry import RetryPolicy
from hooklane.observability.logging import LogEvent, LogLevel, StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics
from hooklane.queue.events import EventStoreUnavailable
from hooklane.queue.events import QueuedEvent


class WorkerQueue(Protocol):
    """Redis operations needed for one normal delivery attempt."""

    async def ensure_consumer_group(self, group_name: str) -> None: ...

    async def read_next(self, group_name: str, consumer_name: str) -> QueuedEvent | None: ...

    async def claim_stale_pending(
        self,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
    ) -> QueuedEvent | None: ...

    async def release_due_retry(self, group_name: str, now_ms: int) -> bool: ...

    async def mark_delivery_started(self, queued_event: QueuedEvent) -> int: ...

    async def mark_delivered(self, queued_event: QueuedEvent, group_name: str) -> None: ...

    async def schedule_retry(
        self,
        queued_event: QueuedEvent,
        due_at_ms: int,
        error_class: str,
    ) -> None: ...

    async def move_to_dead_letter(
        self,
        queued_event: QueuedEvent,
        group_name: str,
        error_class: str,
    ) -> None: ...


class DeliverySink(Protocol):
    """Delivery boundary implemented by the fixed mock-sink client."""

    async def deliver(self, queued_event: QueuedEvent) -> None: ...


@runtime_checkable
class QueueMetricsRefresher(Protocol):
    async def refresh_queue_metrics(self, group_name: str = "hooklane-workers") -> bool: ...


class WorkerResult(StrEnum):
    """Observable result of a single worker iteration."""

    NO_MESSAGE = "no_message"
    DELIVERED = "delivered"
    FAILED_PENDING = "failed_pending"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"


class EventWorker:
    """Process one new stream message, acknowledging only after success."""

    def __init__(
        self,
        queue: WorkerQueue,
        sink: DeliverySink,
        *,
        group_name: str = "hooklane-workers",
        consumer_name: str = "worker-1",
        retry_policy: RetryPolicy | None = None,
        dead_letter_policy: DeadLetterPolicy | None = None,
        pending_idle_ms: int = 60_000,
        clock: Callable[[], float] = time.time,
        random_source: Random | None = None,
        metrics: HooklaneMetrics | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        if pending_idle_ms < 0:
            raise ValueError("pending idle threshold must not be negative")
        self._queue = queue
        self._sink = sink
        self._group_name = group_name
        self._consumer_name = consumer_name
        self._retry_policy = retry_policy
        self._dead_letter_policy = dead_letter_policy
        self._pending_idle_ms = pending_idle_ms
        self._clock = clock
        self._random_source = random_source or Random()
        self._metrics = metrics
        self._logger = logger

    async def _refresh_queue_metrics(self) -> None:
        if isinstance(self._queue, QueueMetricsRefresher):
            await self._queue.refresh_queue_metrics(self._group_name)

    def _finish_observation(
        self,
        queued_event: QueuedEvent,
        *,
        attempt_count: int,
        outcome: str,
        reason_code: str,
        status: str,
        started_at: float,
        event: LogEvent,
        level: LogLevel = LogLevel.INFO,
    ) -> None:
        duration_seconds = time.perf_counter() - started_at
        end_to_end_seconds = None
        if outcome == "success" and queued_event.accepted_at_ms > 0:
            end_to_end_seconds = max(
                0.0,
                self._clock() - (queued_event.accepted_at_ms / 1000),
            )
        if self._metrics is not None:
            self._metrics.finish_delivery(
                outcome=outcome,
                reason_code=reason_code,
                duration_seconds=duration_seconds,
                end_to_end_seconds=end_to_end_seconds,
            )
        if self._logger is not None:
            self._logger.emit(
                event,
                level=level,
                event_id=queued_event.event_id,
                attempt=attempt_count,
                status=status,
                outcome=outcome,
                reason_code=reason_code,
                duration_ms=duration_seconds * 1000,
            )

    async def run_once(self) -> WorkerResult:
        """Run one delivery attempt without acknowledging failed delivery."""

        await self._queue.ensure_consumer_group(self._group_name)
        now_ms = int(self._clock() * 1000)
        await self._queue.release_due_retry(self._group_name, now_ms)
        queued_event = await self._queue.claim_stale_pending(
            self._group_name,
            self._consumer_name,
            self._pending_idle_ms,
        )
        if queued_event is None:
            queued_event = await self._queue.read_next(
                self._group_name,
                self._consumer_name,
            )
        if queued_event is None:
            await self._refresh_queue_metrics()
            return WorkerResult.NO_MESSAGE

        attempt_count = await self._queue.mark_delivery_started(queued_event)
        started_at = time.perf_counter()
        if self._metrics is not None:
            self._metrics.start_delivery()
        if self._logger is not None:
            self._logger.emit(
                LogEvent.DELIVERY_STARTED,
                event_id=queued_event.event_id,
                attempt=attempt_count,
                status="delivering",
                outcome="success",
                reason_code="none",
            )
        try:
            await self._sink.deliver(queued_event)
        except DeliveryFailed as exc:
            if (
                self._dead_letter_policy is not None
                and self._dead_letter_policy.should_dead_letter(
                    exc.error_class,
                    attempt_count,
                )
            ):
                try:
                    await self._queue.move_to_dead_letter(
                        queued_event,
                        self._group_name,
                        exc.error_class.value,
                    )
                except EventStoreUnavailable:
                    self._finish_observation(
                        queued_event,
                        attempt_count=attempt_count,
                        outcome="failure",
                        reason_code="redis_error",
                        status="delivering",
                        started_at=started_at,
                        event=LogEvent.DELIVERY_FAILED,
                        level=LogLevel.ERROR,
                    )
                    raise
                if self._metrics is not None:
                    self._metrics.record_dead_letter(exc.error_class.value)
                self._finish_observation(
                    queued_event,
                    attempt_count=attempt_count,
                    outcome="dead_lettered",
                    reason_code=exc.error_class.value,
                    status="dead_letter",
                    started_at=started_at,
                    event=LogEvent.DEAD_LETTERED,
                    level=LogLevel.ERROR,
                )
                await self._refresh_queue_metrics()
                return WorkerResult.DEAD_LETTERED
            if self._retry_policy is not None and self._retry_policy.is_retryable(
                exc.error_class
            ):
                delay = self._retry_policy.delay_seconds(
                    attempt_count,
                    self._random_source,
                )
                due_at_ms = int((self._clock() + delay) * 1000)
                try:
                    await self._queue.schedule_retry(
                        queued_event,
                        due_at_ms,
                        exc.error_class.value,
                    )
                except EventStoreUnavailable:
                    self._finish_observation(
                        queued_event,
                        attempt_count=attempt_count,
                        outcome="failure",
                        reason_code="redis_error",
                        status="delivering",
                        started_at=started_at,
                        event=LogEvent.DELIVERY_FAILED,
                        level=LogLevel.ERROR,
                    )
                    raise
                if self._metrics is not None:
                    self._metrics.record_retry(exc.error_class.value)
                self._finish_observation(
                    queued_event,
                    attempt_count=attempt_count,
                    outcome="retry_scheduled",
                    reason_code=exc.error_class.value,
                    status="retry_scheduled",
                    started_at=started_at,
                    event=LogEvent.RETRY_SCHEDULED,
                    level=LogLevel.WARNING,
                )
                await self._refresh_queue_metrics()
                return WorkerResult.RETRY_SCHEDULED
            self._finish_observation(
                queued_event,
                attempt_count=attempt_count,
                outcome="pending",
                reason_code=exc.error_class.value,
                status="delivering",
                started_at=started_at,
                event=LogEvent.DELIVERY_FAILED,
                level=LogLevel.ERROR,
            )
            await self._refresh_queue_metrics()
            return WorkerResult.FAILED_PENDING
        except Exception:
            self._finish_observation(
                queued_event,
                attempt_count=attempt_count,
                outcome="failure",
                reason_code="internal_error",
                status="delivering",
                started_at=started_at,
                event=LogEvent.DELIVERY_FAILED,
                level=LogLevel.ERROR,
            )
            raise

        try:
            await self._queue.mark_delivered(queued_event, self._group_name)
        except EventStoreUnavailable:
            self._finish_observation(
                queued_event,
                attempt_count=attempt_count,
                outcome="failure",
                reason_code="redis_error",
                status="delivering",
                started_at=started_at,
                event=LogEvent.DELIVERY_FAILED,
                level=LogLevel.ERROR,
            )
            raise
        self._finish_observation(
            queued_event,
            attempt_count=attempt_count,
            outcome="success",
            reason_code="none",
            status="delivered",
            started_at=started_at,
            event=LogEvent.DELIVERY_COMPLETED,
        )
        await self._refresh_queue_metrics()
        return WorkerResult.DELIVERED
