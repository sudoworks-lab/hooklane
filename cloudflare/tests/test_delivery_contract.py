from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
import json
from time import time
from uuid import UUID, uuid4

import pytest

from hooklane_cf.contract import (
    D1_ROW_LIMIT_BYTES,
    PAYLOAD_CHUNK_LIMIT_BYTES,
    QUEUE_MESSAGE_LIMIT_BYTES,
    EventRequest,
    EventStatus,
    direct_queue_envelope_size,
    payload_json_chunks,
    queue_reference_size,
)
from hooklane_cf.core import (
    AcceptanceRecord,
    AcceptanceService,
    DeliveryDisposition,
    DeliveryErrorClass,
    DeliveryStart,
    DeliveryStartDisposition,
    DeliveryService,
    EventNotFound,
    EventRecord,
    IdempotencyConflict,
    OutboxRecord,
    PersistenceUnavailable,
    QueueSendUnavailable,
    SinkFailure,
    classify_http_status,
)
from hooklane_cf.trace import JsonTrace


@dataclass
class MemoryOutbox:
    state: str
    send_attempt_count: int
    claim_token: str | None = None
    lease_until_ms: int | None = None


class MemoryStore:
    def __init__(self) -> None:
        self.events: dict[UUID, EventRecord] = {}
        self.idempotency: dict[str, tuple[str, UUID]] = {}
        self.outbox: dict[UUID, MemoryOutbox] = {}
        self.delivery_tokens: dict[UUID, str | None] = {}
        self.delivery_leases: dict[UUID, int | None] = {}
        self.lock = asyncio.Lock()
        self.fail_accept = False
        self.fail_dispatch_once = False
        self.fail_mark_delivered_once = False

    async def accept(
        self,
        proposed_event_id: UUID,
        event: EventRequest,
        key_hash: str | None,
        fingerprint: str,
    ) -> AcceptanceRecord:
        if self.fail_accept:
            raise PersistenceUnavailable
        async with self.lock:
            if key_hash is not None and key_hash in self.idempotency:
                stored_fingerprint, event_id = self.idempotency[key_hash]
                if stored_fingerprint != fingerprint:
                    raise IdempotencyConflict
                stored = self.events[event_id]
                return AcceptanceRecord(
                    event_id=event_id,
                    created=False,
                    status=stored.status,
                    attempt_count=stored.attempt_count,
                )
            self.events[proposed_event_id] = EventRecord(
                event_id=proposed_event_id,
                event=event,
                status=EventStatus.QUEUED,
                attempt_count=0,
            )
            self.outbox[proposed_event_id] = MemoryOutbox("pending", 0)
            self.delivery_tokens[proposed_event_id] = None
            self.delivery_leases[proposed_event_id] = None
            if key_hash is not None:
                self.idempotency[key_hash] = (fingerprint, proposed_event_id)
            return AcceptanceRecord(event_id=proposed_event_id, created=True)

    async def get_status(self, event_id: UUID) -> EventRecord | None:
        return self.events.get(event_id)

    async def claim_outbox(
        self,
        limit: int,
        claim_token: str,
        lease_ms: int,
    ) -> tuple[OutboxRecord, ...]:
        now_ms = int(time() * 1000)
        claimed: list[OutboxRecord] = []
        async with self.lock:
            for event_id, outbox in self.outbox.items():
                if len(claimed) >= limit:
                    break
                claim_expired = (
                    outbox.lease_until_ms is not None and outbox.lease_until_ms <= now_ms
                )
                if outbox.state != "pending" or (
                    outbox.claim_token is not None and not claim_expired
                ):
                    continue
                outbox.claim_token = claim_token
                outbox.lease_until_ms = now_ms + lease_ms
                claimed.append(
                    OutboxRecord(
                        event_id=event_id,
                        send_attempt_count=outbox.send_attempt_count,
                        claim_token=claim_token,
                    )
                )
        return tuple(claimed)

    async def mark_dispatched(
        self,
        event_id: UUID,
        claim_token: str | None = None,
    ) -> bool:
        if self.fail_dispatch_once:
            self.fail_dispatch_once = False
            raise PersistenceUnavailable
        async with self.lock:
            outbox = self.outbox[event_id]
            if outbox.state != "pending" or outbox.claim_token != claim_token:
                return False
            outbox.state = "sent"
            outbox.send_attempt_count += 1
            outbox.claim_token = None
            outbox.lease_until_ms = None
            return True

    async def release_outbox_claim(self, event_id: UUID, claim_token: str) -> None:
        async with self.lock:
            outbox = self.outbox[event_id]
            if outbox.state == "pending" and outbox.claim_token == claim_token:
                outbox.claim_token = None
                outbox.lease_until_ms = None

    async def mark_delivery_started(
        self,
        event_id: UUID,
        maximum_attempts: int,
    ) -> DeliveryStart:
        now_ms = int(time() * 1000)
        async with self.lock:
            stored = self.events.get(event_id)
            if stored is None:
                raise EventNotFound
            if stored.status in {EventStatus.DELIVERED, EventStatus.DEAD_LETTER}:
                return DeliveryStart(
                    disposition=DeliveryStartDisposition.TERMINAL,
                    status=stored.status,
                    attempt_count=stored.attempt_count,
                )
            lease_until = self.delivery_leases[event_id]
            if stored.status is EventStatus.DELIVERING and lease_until is not None and lease_until > now_ms:
                return DeliveryStart(
                    disposition=DeliveryStartDisposition.BUSY,
                    status=stored.status,
                    attempt_count=stored.attempt_count,
                )
            if stored.status is EventStatus.DELIVERING:
                attempt_count = (
                    stored.attempt_count + 1
                    if stored.attempt_count < maximum_attempts
                    else stored.attempt_count
                )
            elif stored.attempt_count < maximum_attempts:
                attempt_count = stored.attempt_count + 1
            else:
                self.events[event_id] = EventRecord(
                    event_id=event_id,
                    event=stored.event,
                    status=EventStatus.DEAD_LETTER,
                    attempt_count=stored.attempt_count,
                )
                self.delivery_tokens[event_id] = None
                self.delivery_leases[event_id] = None
                return DeliveryStart(
                    disposition=DeliveryStartDisposition.TERMINAL,
                    status=EventStatus.DEAD_LETTER,
                    attempt_count=stored.attempt_count,
                )
            if attempt_count > maximum_attempts:
                raise PersistenceUnavailable
            token = uuid4().hex
            updated = EventRecord(
                event_id=event_id,
                event=stored.event,
                status=EventStatus.DELIVERING,
                attempt_count=attempt_count,
            )
            self.events[event_id] = updated
            self.delivery_tokens[event_id] = token
            self.delivery_leases[event_id] = now_ms + 30_000
            return DeliveryStart(
                disposition=DeliveryStartDisposition.STARTED,
                status=updated.status,
                attempt_count=updated.attempt_count,
                record=updated,
                delivery_token=token,
            )

    async def mark_delivered(self, event_id: UUID, delivery_token: str) -> bool:
        if self.fail_mark_delivered_once:
            self.fail_mark_delivered_once = False
            raise PersistenceUnavailable
        return await self._set_status(event_id, EventStatus.DELIVERED, delivery_token)

    async def mark_retry(
        self,
        event_id: UUID,
        _reason: DeliveryErrorClass,
        delivery_token: str,
    ) -> bool:
        return await self._set_status(event_id, EventStatus.RETRY_SCHEDULED, delivery_token)

    async def mark_dead_letter(
        self,
        event_id: UUID,
        _reason: DeliveryErrorClass,
        delivery_token: str,
    ) -> bool:
        return await self._set_status(event_id, EventStatus.DEAD_LETTER, delivery_token)

    async def _set_status(
        self,
        event_id: UUID,
        status: EventStatus,
        delivery_token: str,
    ) -> bool:
        async with self.lock:
            stored = self.events[event_id]
            if (
                stored.status is not EventStatus.DELIVERING
                or self.delivery_tokens[event_id] != delivery_token
            ):
                return False
            self.events[event_id] = EventRecord(
                event_id=stored.event_id,
                event=stored.event,
                status=status,
                attempt_count=stored.attempt_count,
            )
            self.delivery_tokens[event_id] = None
            self.delivery_leases[event_id] = None
            return True

    async def expire_delivery_lease(self, event_id: UUID) -> None:
        async with self.lock:
            self.delivery_leases[event_id] = 0


class MemoryQueue:
    def __init__(self, *, failures: int = 0, delay_seconds: float = 0.0) -> None:
        self.failures = failures
        self.delay_seconds = delay_seconds
        self.messages: list[UUID] = []

    async def send(self, event_id: UUID) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise QueueSendUnavailable
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.messages.append(event_id)


class SequenceSink:
    def __init__(self, outcomes: Iterable[DeliveryErrorClass | None]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[UUID] = []

    async def deliver(self, record: EventRecord) -> None:
        self.calls.append(record.event_id)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise SinkFailure(outcome)


class CoordinatedSink:
    def __init__(self, outcomes: Iterable[DeliveryErrorClass | None]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[UUID] = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def deliver(self, record: EventRecord) -> None:
        self.calls.append(record.event_id)
        if len(self.calls) == 1:
            self.first_started.set()
            await self.release_first.wait()
        outcome = self.outcomes[len(self.calls) - 1]
        if outcome is not None:
            raise SinkFailure(outcome)


class RecordingTrace:
    def __init__(self) -> None:
        self.records: list[dict[str, str | int]] = []

    def emit(
        self,
        transition: str,
        *,
        event_id: UUID,
        attempt: int,
        reason: str,
    ) -> None:
        self.records.append(
            {
                "attempt": attempt,
                "event_id": str(event_id),
                "reason": reason,
                "transition": transition,
            }
        )


def event(payload: dict[str, object] | None = None) -> EventRequest:
    return EventRequest(event_type="delivery.test", payload=payload or {"message": "accepted"})


async def accepted(
    store: MemoryStore,
    queue: MemoryQueue,
    *,
    trace: RecordingTrace | None = None,
) -> UUID:
    record = await AcceptanceService(store, queue, trace).accept(event(), "stable-key")
    return record.event_id


@pytest.mark.asyncio
async def test_normal_delivery_preserves_status_and_attempt_contract() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    trace = RecordingTrace()
    event_id = await accepted(store, queue, trace=trace)

    result = await DeliveryService(store, SequenceSink([None]), trace=trace).process(event_id)

    assert result.disposition is DeliveryDisposition.ACK
    assert result.status is EventStatus.DELIVERED
    assert store.events[event_id].status is EventStatus.DELIVERED
    assert store.events[event_id].attempt_count == 1
    assert [item["transition"] for item in trace.records] == [
        "accepted",
        "queued",
        "delivering",
        "delivered",
    ]


@pytest.mark.asyncio
async def test_idempotent_reuse_returns_same_event_without_duplicate_enqueue() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    service = AcceptanceService(store, queue)

    first = await service.accept(event(), "same-key")
    second = await service.accept(event(), "same-key")

    assert first.event_id == second.event_id
    assert second.created is False
    assert queue.messages == [first.event_id]


@pytest.mark.asyncio
async def test_concurrent_idempotency_creates_one_logical_event() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    service = AcceptanceService(store, queue)

    records = await asyncio.gather(*(service.accept(event(), "concurrent-key") for _ in range(20)))

    assert len({record.event_id for record in records}) == 1
    assert sum(record.created for record in records) == 1
    assert len(store.events) == 1
    assert len(queue.messages) == 1


@pytest.mark.asyncio
async def test_idempotency_conflict_is_rejected_without_second_event() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    service = AcceptanceService(store, queue)
    await service.accept(event({"version": 1}), "conflict-key")

    with pytest.raises(IdempotencyConflict):
        await service.accept(event({"version": 2}), "conflict-key")

    assert len(store.events) == 1
    assert len(queue.messages) == 1


@pytest.mark.asyncio
async def test_retryable_5xx_retries_same_event_then_delivers() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    event_id = await accepted(store, queue)
    service = DeliveryService(
        store,
        SequenceSink([DeliveryErrorClass.HTTP_5XX, None]),
    )

    first = await service.process(event_id)
    second = await service.process(event_id)

    assert first.disposition is DeliveryDisposition.RETRY
    assert first.status is EventStatus.RETRY_SCHEDULED
    assert first.retry_delay_seconds is not None
    assert second.event_id == event_id
    assert second.status is EventStatus.DELIVERED
    assert store.events[event_id].attempt_count == 2


@pytest.mark.asyncio
async def test_non_retryable_4xx_dead_letters_without_retry() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    event_id = await accepted(store, queue)
    result = await DeliveryService(
        store,
        SequenceSink([DeliveryErrorClass.HTTP_4XX]),
    ).process(event_id)

    assert result.disposition is DeliveryDisposition.ACK
    assert result.status is EventStatus.DEAD_LETTER
    assert result.retry_delay_seconds is None
    assert store.events[event_id].attempt_count == 1


@pytest.mark.asyncio
async def test_maximum_attempts_is_terminal_dead_letter() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    event_id = await accepted(store, queue)
    service = DeliveryService(
        store,
        SequenceSink([DeliveryErrorClass.HTTP_5XX] * 5),
        maximum_attempts=5,
    )

    results = [await service.process(event_id) for _ in range(5)]

    assert [result.disposition for result in results[:4]] == [DeliveryDisposition.RETRY] * 4
    assert results[4].disposition is DeliveryDisposition.ACK
    assert results[4].status is EventStatus.DEAD_LETTER
    assert store.events[event_id].attempt_count == 5


@pytest.mark.asyncio
async def test_dead_letter_redelivery_is_terminal_and_does_not_call_sink() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    event_id = await accepted(store, queue)
    sink = SequenceSink([DeliveryErrorClass.HTTP_5XX] * 5)
    service = DeliveryService(store, sink, maximum_attempts=5)

    for _ in range(5):
        await service.process(event_id)
    result = await service.process(event_id)

    assert result.disposition is DeliveryDisposition.ACK
    assert result.status is EventStatus.DEAD_LETTER
    assert sink.calls == [event_id] * 5
    assert store.events[event_id].attempt_count == 5


@pytest.mark.asyncio
async def test_delivered_redelivery_is_terminal_and_does_not_call_sink() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    event_id = await accepted(store, queue)
    sink = SequenceSink([None])
    service = DeliveryService(store, sink)

    first = await service.process(event_id)
    second = await service.process(event_id)

    assert first.status is EventStatus.DELIVERED
    assert second.disposition is DeliveryDisposition.ACK
    assert second.status is EventStatus.DELIVERED
    assert sink.calls == [event_id]
    assert store.events[event_id].attempt_count == 1


@pytest.mark.asyncio
async def test_newer_success_wins_over_stale_failure() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    event_id = await accepted(store, queue)
    sink = CoordinatedSink([DeliveryErrorClass.HTTP_5XX, None])
    service = DeliveryService(store, sink)

    stale_task = asyncio.create_task(service.process(event_id))
    await sink.first_started.wait()
    await store.expire_delivery_lease(event_id)
    newer = await service.process(event_id)
    sink.release_first.set()
    stale = await stale_task

    assert newer.status is EventStatus.DELIVERED
    assert stale.status is EventStatus.DELIVERED
    assert store.events[event_id].status is EventStatus.DELIVERED
    assert store.events[event_id].attempt_count == 2


@pytest.mark.asyncio
async def test_stale_success_cannot_overwrite_newer_retry() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    event_id = await accepted(store, queue)
    sink = CoordinatedSink([None, DeliveryErrorClass.HTTP_5XX])
    service = DeliveryService(store, sink)

    stale_task = asyncio.create_task(service.process(event_id))
    await sink.first_started.wait()
    await store.expire_delivery_lease(event_id)
    newer = await service.process(event_id)
    sink.release_first.set()
    stale = await stale_task

    assert newer.status is EventStatus.RETRY_SCHEDULED
    assert stale.status is EventStatus.RETRY_SCHEDULED
    assert store.events[event_id].status is EventStatus.RETRY_SCHEDULED
    assert store.events[event_id].attempt_count == 2


@pytest.mark.asyncio
async def test_success_before_state_transition_failure_exposes_duplicate_boundary() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    event_id = await accepted(store, queue)
    store.fail_mark_delivered_once = True
    sink = SequenceSink([None, None])
    service = DeliveryService(store, sink)

    with pytest.raises(PersistenceUnavailable):
        await service.process(event_id)
    await store.expire_delivery_lease(event_id)
    result = await service.process(event_id)

    assert result.status is EventStatus.DELIVERED
    assert sink.calls == [event_id, event_id]
    assert store.events[event_id].attempt_count == 2


@pytest.mark.asyncio
async def test_persistence_failure_never_returns_false_acceptance() -> None:
    store = MemoryStore()
    store.fail_accept = True
    queue = MemoryQueue()

    with pytest.raises(PersistenceUnavailable):
        await AcceptanceService(store, queue).accept(event(), "key")

    assert queue.messages == []
    assert store.events == {}


@pytest.mark.asyncio
async def test_queue_send_failure_keeps_repairable_outbox() -> None:
    store = MemoryStore()
    queue = MemoryQueue(failures=1)
    service = AcceptanceService(store, queue)

    record = await service.accept(event(), "key")
    assert record.status is EventStatus.QUEUED
    assert store.outbox[record.event_id] == MemoryOutbox("pending", 0)
    assert queue.messages == []

    repaired, deferred = await service.repair()
    assert (repaired, deferred) == (1, 0)
    assert queue.messages == [record.event_id]
    assert store.outbox[record.event_id] == MemoryOutbox("sent", 1)


@pytest.mark.asyncio
async def test_concurrent_outbox_repair_claims_once_without_event_loss() -> None:
    store = MemoryStore()
    queue = MemoryQueue(failures=1, delay_seconds=0.01)
    service = AcceptanceService(store, queue)
    record = await service.accept(event(), "concurrent-repair-key")

    outcomes = await asyncio.gather(*(service.repair() for _ in range(20)))

    assert sum(repaired for repaired, _deferred in outcomes) == 1
    assert sum(deferred for _repaired, deferred in outcomes) == 0
    assert queue.messages == [record.event_id]
    assert store.outbox[record.event_id] == MemoryOutbox("sent", 1)


@pytest.mark.asyncio
async def test_expired_outbox_claim_can_be_repaired_at_least_once() -> None:
    store = MemoryStore()
    queue = MemoryQueue(failures=1)
    service = AcceptanceService(store, queue)
    record = await service.accept(event(), "expired-claim-key")
    claimed = await store.claim_outbox(1, "abandoned-owner", 30_000)
    assert [item.event_id for item in claimed] == [record.event_id]
    store.outbox[record.event_id].lease_until_ms = 0

    repaired, deferred = await service.repair()

    assert (repaired, deferred) == (1, 0)
    assert queue.messages == [record.event_id]
    assert store.outbox[record.event_id] == MemoryOutbox("sent", 1)


@pytest.mark.asyncio
async def test_stale_outbox_owner_cannot_overwrite_new_claim() -> None:
    store = MemoryStore()
    queue = MemoryQueue(failures=1)
    record = await AcceptanceService(store, queue).accept(event(), "claim-cas-key")
    first = await store.claim_outbox(1, "first-owner", 30_000)
    assert [item.event_id for item in first] == [record.event_id]
    store.outbox[record.event_id].lease_until_ms = 0
    second = await store.claim_outbox(1, "second-owner", 30_000)
    assert [item.event_id for item in second] == [record.event_id]

    assert await store.mark_dispatched(record.event_id, "first-owner") is False
    assert await store.mark_dispatched(record.event_id, "second-owner") is True
    assert store.outbox[record.event_id] == MemoryOutbox("sent", 1)


@pytest.mark.asyncio
async def test_send_success_then_transition_failure_is_loss_safe_and_may_duplicate() -> None:
    store = MemoryStore()
    store.fail_dispatch_once = True
    queue = MemoryQueue()
    service = AcceptanceService(store, queue)

    record = await service.accept(event(), "key")
    assert queue.messages == [record.event_id]
    assert store.outbox[record.event_id] == MemoryOutbox("pending", 0)

    repaired, deferred = await service.repair()
    assert (repaired, deferred) == (1, 0)
    assert queue.messages == [record.event_id, record.event_id]
    assert store.outbox[record.event_id] == MemoryOutbox("sent", 1)


def test_payload_boundary_is_removed_from_queue_and_d1_single_row() -> None:
    event_id = uuid4()
    below = event({"data": "x" * (QUEUE_MESSAGE_LIMIT_BYTES - 1024)})
    above_queue = event({"data": "x" * (QUEUE_MESSAGE_LIMIT_BYTES + 1024)})
    above_d1_row = event({"data": "界" * (D1_ROW_LIMIT_BYTES // 3 + 1024)})
    chunks = payload_json_chunks(above_d1_row)

    assert direct_queue_envelope_size(event_id, below) < QUEUE_MESSAGE_LIMIT_BYTES
    assert direct_queue_envelope_size(event_id, above_queue) >= QUEUE_MESSAGE_LIMIT_BYTES
    assert queue_reference_size(event_id) < QUEUE_MESSAGE_LIMIT_BYTES
    assert len(above_d1_row.model_dump_json().encode("utf-8")) > D1_ROW_LIMIT_BYTES
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= PAYLOAD_CHUNK_LIMIT_BYTES for chunk in chunks)
    assert json.loads("".join(chunks)) == above_d1_row.payload


def test_http_classification_matches_delivery_contract() -> None:
    assert classify_http_status(204) is None
    assert classify_http_status(301) is DeliveryErrorClass.HTTP_3XX
    assert classify_http_status(400) is DeliveryErrorClass.HTTP_4XX
    assert classify_http_status(429) is DeliveryErrorClass.HTTP_429
    assert classify_http_status(503) is DeliveryErrorClass.HTTP_5XX


@pytest.mark.asyncio
async def test_trace_never_contains_payload_key_credential_or_exception_text() -> None:
    payload_marker = "payload-private-marker"
    key_marker = "raw-idempotency-private-marker"
    credential_marker = "credential-private-marker"
    store = MemoryStore()
    queue = MemoryQueue(failures=1)
    trace = RecordingTrace()

    await AcceptanceService(store, queue, trace).accept(
        event({"message": payload_marker, "credential": credential_marker}),
        key_marker,
    )
    serialized = json.dumps(trace.records, sort_keys=True)

    assert payload_marker not in serialized
    assert key_marker not in serialized
    assert credential_marker not in serialized
    assert set().union(*(record.keys() for record in trace.records)) == {
        "attempt",
        "event_id",
        "reason",
        "transition",
    }


def test_status_vocabulary_is_exact() -> None:
    observed: dict[str, int] = defaultdict(int)
    for status in EventStatus:
        observed[status.value] += 1
    assert observed == {
        "queued": 1,
        "delivering": 1,
        "retry_scheduled": 1,
        "delivered": 1,
        "dead_letter": 1,
    }


@pytest.mark.asyncio
async def test_production_json_trace_is_content_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    queue = MemoryQueue(failures=1)
    payload_marker = "payload-private-marker"
    key_marker = "raw-idempotency-private-marker"
    credential_marker = "credential-private-marker"
    unsafe_exception_marker = "unsafe-exception-private-marker"
    trace = JsonTrace()
    accepted_record = await AcceptanceService(store, queue, trace).accept(
        event(
            {
                "message": payload_marker,
                "credential": credential_marker,
                "exception": unsafe_exception_marker,
            }
        ),
        key_marker,
    )
    event_id = accepted_record.event_id
    await DeliveryService(
        store,
        SequenceSink([DeliveryErrorClass.HTTP_5XX]),
        trace=trace,
    ).process(event_id)
    output = capsys.readouterr().out

    for marker in (
        payload_marker,
        key_marker,
        credential_marker,
        unsafe_exception_marker,
    ):
        assert marker not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert records
    assert all(
        set(record) == {"attempt", "event_id", "reason", "transition"}
        for record in records
    )
