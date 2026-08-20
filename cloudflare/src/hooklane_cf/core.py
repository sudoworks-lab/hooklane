"""Provider-neutral acceptance, outbox repair, and delivery state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from random import Random
from typing import Protocol
from uuid import UUID, uuid4

from hooklane_cf.contract import EventRequest, EventStatus, idempotency_key_hash
from hooklane_cf.contract import request_fingerprint


class PersistenceUnavailable(Exception):
    """Raised when durable D1 state cannot be read or written."""


class QueueSendUnavailable(Exception):
    """Raised when a Queue message was not confirmed durable."""


class IdempotencyConflict(Exception):
    """Raised when a key is reused with different canonical content."""


class EventNotFound(Exception):
    """Raised when a Queue message references no durable event."""


class DeliveryStartDisposition(StrEnum):
    """Describe the result of claiming a delivery attempt."""

    STARTED = "started"
    BUSY = "busy"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class DeliveryStart:
    """D1-backed delivery claim or a safe duplicate disposition."""

    disposition: DeliveryStartDisposition
    status: EventStatus
    attempt_count: int
    record: EventRecord | None = None
    delivery_token: str | None = None


class DeliveryErrorClass(StrEnum):
    """Content-free delivery failure classifications."""

    TIMEOUT = "timeout"
    CONNECTION = "connection_error"
    HTTP_1XX = "http_1xx"
    HTTP_3XX = "http_3xx"
    HTTP_429 = "http_429"
    HTTP_5XX = "http_5xx"
    HTTP_4XX = "http_4xx"


class SinkFailure(Exception):
    """Fixed delivery failure with no downstream exception text."""

    def __init__(self, error_class: DeliveryErrorClass) -> None:
        super().__init__(error_class.value)
        self.error_class = error_class


@dataclass(frozen=True)
class AcceptanceRecord:
    event_id: UUID
    created: bool
    status: EventStatus = EventStatus.QUEUED
    attempt_count: int = 0


@dataclass(frozen=True)
class EventRecord:
    event_id: UUID
    event: EventRequest
    status: EventStatus
    attempt_count: int


@dataclass(frozen=True)
class OutboxRecord:
    event_id: UUID
    send_attempt_count: int
    claim_token: str


OUTBOX_LEASE_MS = 30_000


class EventStore(Protocol):
    async def accept(
        self,
        proposed_event_id: UUID,
        event: EventRequest,
        key_hash: str | None,
        fingerprint: str,
    ) -> AcceptanceRecord: ...

    async def get_status(self, event_id: UUID) -> EventRecord | None: ...

    async def claim_outbox(
        self,
        limit: int,
        claim_token: str,
        lease_ms: int,
    ) -> tuple[OutboxRecord, ...]: ...

    async def mark_dispatched(self, event_id: UUID, claim_token: str | None = None) -> bool: ...

    async def release_outbox_claim(self, event_id: UUID, claim_token: str) -> None: ...

    async def mark_delivery_started(
        self,
        event_id: UUID,
        maximum_attempts: int,
    ) -> DeliveryStart: ...

    async def mark_delivered(self, event_id: UUID, delivery_token: str) -> bool: ...

    async def mark_retry(
        self,
        event_id: UUID,
        reason: DeliveryErrorClass,
        delivery_token: str,
    ) -> bool: ...

    async def mark_dead_letter(
        self,
        event_id: UUID,
        reason: DeliveryErrorClass,
        delivery_token: str,
    ) -> bool: ...


class QueueProducer(Protocol):
    async def send(self, event_id: UUID) -> None: ...


class DeliverySink(Protocol):
    async def deliver(self, record: EventRecord) -> None: ...


class Trace(Protocol):
    def emit(
        self,
        transition: str,
        *,
        event_id: UUID,
        attempt: int,
        reason: str,
    ) -> None: ...


class NullTrace:
    def emit(
        self,
        transition: str,
        *,
        event_id: UUID,
        attempt: int,
        reason: str,
    ) -> None:
        return None


class AcceptanceService:
    """Persist before 202 and bridge the D1-to-Queue atomicity gap."""

    def __init__(self, store: EventStore, queue: QueueProducer, trace: Trace | None = None) -> None:
        self._store = store
        self._queue = queue
        self._trace = trace or NullTrace()

    async def accept(
        self,
        event: EventRequest,
        idempotency_key: str | None,
        *,
        proposed_event_id: UUID | None = None,
    ) -> AcceptanceRecord:
        event_id = proposed_event_id or uuid4()
        record = await self._store.accept(
            event_id,
            event,
            idempotency_key_hash(idempotency_key),
            request_fingerprint(event),
        )
        if not record.created:
            self._trace.emit(
                "idempotent_reuse",
                event_id=record.event_id,
                attempt=record.attempt_count,
                reason="none",
            )
            return record

        self._trace.emit(
            "accepted",
            event_id=record.event_id,
            attempt=0,
            reason="none",
        )
        try:
            await self._queue.send(record.event_id)
        except QueueSendUnavailable:
            self._trace.emit(
                "outbox_pending",
                event_id=record.event_id,
                attempt=0,
                reason="queue_unavailable",
            )
            return record

        try:
            dispatched = await self._store.mark_dispatched(record.event_id)
        except PersistenceUnavailable:
            self._trace.emit(
                "outbox_pending",
                event_id=record.event_id,
                attempt=0,
                reason="dispatch_transition_failed",
            )
            return record
        if not dispatched:
            self._trace.emit(
                "outbox_pending",
                event_id=record.event_id,
                attempt=0,
                reason="dispatch_claimed",
            )
            return record

        self._trace.emit(
            "queued",
            event_id=record.event_id,
            attempt=0,
            reason="none",
        )
        return record

    async def repair(self, limit: int = 100) -> tuple[int, int]:
        """Claim and resend pending references; expired claims may be duplicated."""

        repaired = 0
        deferred = 0
        claim_token = uuid4().hex
        outboxes = await self._store.claim_outbox(limit, claim_token, OUTBOX_LEASE_MS)
        for outbox in outboxes:
            try:
                await self._queue.send(outbox.event_id)
            except QueueSendUnavailable:
                try:
                    await self._store.release_outbox_claim(outbox.event_id, claim_token)
                except PersistenceUnavailable:
                    pass
                deferred += 1
                self._trace.emit(
                    "repair_deferred",
                    event_id=outbox.event_id,
                    attempt=outbox.send_attempt_count,
                    reason="dependency_unavailable",
                )
                continue
            try:
                dispatched = await self._store.mark_dispatched(outbox.event_id, claim_token)
            except PersistenceUnavailable:
                deferred += 1
                self._trace.emit(
                    "repair_deferred",
                    event_id=outbox.event_id,
                    attempt=outbox.send_attempt_count,
                    reason="dispatch_transition_failed",
                )
                continue
            if not dispatched:
                deferred += 1
                self._trace.emit(
                    "repair_deferred",
                    event_id=outbox.event_id,
                    attempt=outbox.send_attempt_count,
                    reason="claim_lost",
                )
                continue
            repaired += 1
            self._trace.emit(
                "repair_dispatched",
                event_id=outbox.event_id,
                attempt=outbox.send_attempt_count + 1,
                reason="none",
            )
        return repaired, deferred


class DeliveryDisposition(StrEnum):
    ACK = "ack"
    RETRY = "retry"


@dataclass(frozen=True)
class DeliveryResult:
    disposition: DeliveryDisposition
    event_id: UUID
    attempt_count: int
    status: EventStatus
    retry_delay_seconds: int | None = None


class DeliveryService:
    """Run one at-least-once delivery attempt for a Queue reference."""

    def __init__(
        self,
        store: EventStore,
        sink: DeliverySink,
        *,
        maximum_attempts: int = 5,
        random_source: Random | None = None,
        trace: Trace | None = None,
    ) -> None:
        if maximum_attempts < 1:
            raise ValueError("maximum attempts must be positive")
        self._store = store
        self._sink = sink
        self._maximum_attempts = maximum_attempts
        self._random = random_source or Random()
        self._trace = trace or NullTrace()

    @staticmethod
    def is_retryable(error_class: DeliveryErrorClass) -> bool:
        return error_class in {
            DeliveryErrorClass.TIMEOUT,
            DeliveryErrorClass.CONNECTION,
            DeliveryErrorClass.HTTP_429,
            DeliveryErrorClass.HTTP_5XX,
        }

    def retry_delay_seconds(self, attempt_count: int) -> int:
        if attempt_count < 1:
            raise ValueError("attempt count must be positive")
        backoff = min(2 ** min(attempt_count - 1, 62), 60)
        jittered = backoff * self._random.uniform(0.75, 1.25)
        return int(max(1, min(round(jittered), 60)))

    async def _current_result(self, event_id: UUID) -> DeliveryResult:
        current = await self._store.get_status(event_id)
        if current is None:
            raise EventNotFound
        if current.status in {EventStatus.DELIVERED, EventStatus.DEAD_LETTER}:
            return DeliveryResult(
                disposition=DeliveryDisposition.ACK,
                event_id=event_id,
                attempt_count=current.attempt_count,
                status=current.status,
            )
        return DeliveryResult(
            disposition=DeliveryDisposition.RETRY,
            event_id=event_id,
            attempt_count=current.attempt_count,
            status=current.status,
            retry_delay_seconds=1,
        )

    async def process(self, event_id: UUID) -> DeliveryResult:
        start = await self._store.mark_delivery_started(event_id, self._maximum_attempts)
        if start.disposition is DeliveryStartDisposition.TERMINAL:
            self._trace.emit(
                "terminal_duplicate",
                event_id=event_id,
                attempt=start.attempt_count,
                reason="terminal_state",
            )
            return DeliveryResult(
                disposition=DeliveryDisposition.ACK,
                event_id=event_id,
                attempt_count=start.attempt_count,
                status=start.status,
            )
        if start.disposition is DeliveryStartDisposition.BUSY:
            self._trace.emit(
                "delivery_deferred",
                event_id=event_id,
                attempt=start.attempt_count,
                reason="active_attempt",
            )
            return DeliveryResult(
                disposition=DeliveryDisposition.RETRY,
                event_id=event_id,
                attempt_count=start.attempt_count,
                status=start.status,
                retry_delay_seconds=1,
            )
        if start.record is None or start.delivery_token is None:
            raise PersistenceUnavailable
        record = start.record
        delivery_token = start.delivery_token
        self._trace.emit(
            "delivering",
            event_id=event_id,
            attempt=record.attempt_count,
            reason="none",
        )
        try:
            await self._sink.deliver(record)
        except SinkFailure as failure:
            if (
                not self.is_retryable(failure.error_class)
                or record.attempt_count >= self._maximum_attempts
            ):
                transitioned = await self._store.mark_dead_letter(
                    event_id,
                    failure.error_class,
                    delivery_token,
                )
                if not transitioned:
                    self._trace.emit(
                        "delivery_stale",
                        event_id=event_id,
                        attempt=record.attempt_count,
                        reason="ownership_lost",
                    )
                    return await self._current_result(event_id)
                self._trace.emit(
                    "dead_letter",
                    event_id=event_id,
                    attempt=record.attempt_count,
                    reason=failure.error_class.value,
                )
                return DeliveryResult(
                    disposition=DeliveryDisposition.ACK,
                    event_id=event_id,
                    attempt_count=record.attempt_count,
                    status=EventStatus.DEAD_LETTER,
                )

            transitioned = await self._store.mark_retry(
                event_id,
                failure.error_class,
                delivery_token,
            )
            if not transitioned:
                self._trace.emit(
                    "delivery_stale",
                    event_id=event_id,
                    attempt=record.attempt_count,
                    reason="ownership_lost",
                )
                return await self._current_result(event_id)
            delay = self.retry_delay_seconds(record.attempt_count)
            self._trace.emit(
                "retry_scheduled",
                event_id=event_id,
                attempt=record.attempt_count,
                reason=failure.error_class.value,
            )
            return DeliveryResult(
                disposition=DeliveryDisposition.RETRY,
                event_id=event_id,
                attempt_count=record.attempt_count,
                status=EventStatus.RETRY_SCHEDULED,
                retry_delay_seconds=delay,
            )

        transitioned = await self._store.mark_delivered(event_id, delivery_token)
        if not transitioned:
            self._trace.emit(
                "delivery_stale",
                event_id=event_id,
                attempt=record.attempt_count,
                reason="ownership_lost",
            )
            return await self._current_result(event_id)
        self._trace.emit(
            "delivered",
            event_id=event_id,
            attempt=record.attempt_count,
            reason="none",
        )
        return DeliveryResult(
            disposition=DeliveryDisposition.ACK,
            event_id=event_id,
            attempt_count=record.attempt_count,
            status=EventStatus.DELIVERED,
        )


def classify_http_status(status_code: int) -> DeliveryErrorClass | None:
    """Map downstream HTTP status to the existing Hooklane contract."""

    if 200 <= status_code < 300:
        return None
    if 100 <= status_code < 200:
        return DeliveryErrorClass.HTTP_1XX
    if 300 <= status_code < 400:
        return DeliveryErrorClass.HTTP_3XX
    if status_code == 429:
        return DeliveryErrorClass.HTTP_429
    if status_code >= 500:
        return DeliveryErrorClass.HTTP_5XX
    return DeliveryErrorClass.HTTP_4XX
