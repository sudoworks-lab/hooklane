"""Python Worker ingress and Queue consumer for the local Cloudflare spike."""

from __future__ import annotations

import asyncio
from http import HTTPMethod
import json
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

import asgi  # type: ignore[import-untyped]
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from workers import WorkerEntrypoint, fetch

from hooklane_cf.contract import (
    EventAccepted,
    EventRequest,
    EventStatus,
    EventStatusResponse,
    request_fingerprint,
)
from hooklane_cf.core import (
    AcceptanceService,
    DeliveryDisposition,
    DeliveryErrorClass,
    DeliveryService,
    EventNotFound,
    IdempotencyConflict,
    PersistenceUnavailable,
    QueueSendUnavailable,
    SinkFailure,
    classify_http_status,
)
from hooklane_cf.d1 import D1EventStore
from hooklane_cf.trace import JsonTrace


class CloudflareQueueProducer:
    def __init__(self, binding: Any, *, fail_send: bool = False) -> None:
        self._binding = binding
        self._fail_send = fail_send

    async def send(self, event_id: UUID) -> None:
        if self._fail_send:
            self._fail_send = False
            raise QueueSendUnavailable
        try:
            await self._binding.send({"event_id": str(event_id)})
        except Exception:
            raise QueueSendUnavailable from None


class FixedMockSink:
    """Deliver only to the operator-controlled URL in Wrangler configuration."""

    def __init__(self, destination_url: str) -> None:
        parsed = urlparse(destination_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("mock sink URL must be absolute HTTP(S)")
        self._destination_url = destination_url

    async def deliver(self, record: Any) -> None:
        body = json.dumps(
            {
                "event_id": str(record.event_id),
                "event_type": record.event.event_type,
                "payload": record.event.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            response = await asyncio.wait_for(
                fetch(
                    self._destination_url,
                    method=HTTPMethod.POST,
                    headers={"content-type": "application/json"},
                    body=body,
                    redirect="manual",
                ),
                timeout=5.0,
            )
        except TimeoutError:
            raise SinkFailure(DeliveryErrorClass.TIMEOUT) from None
        except Exception:
            raise SinkFailure(DeliveryErrorClass.CONNECTION) from None
        error_class = classify_http_status(int(response.status))
        if error_class is not None:
            raise SinkFailure(error_class)


class DeliveryRegressionRequest(BaseModel):
    """Local-only state-machine scenario selector."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario: Literal[
        "dead_letter_redelivery",
        "delivered_redelivery",
        "concurrent_stale_failure",
        "stale_success",
        "delivery_transition_failure",
    ]


class RegressionSink:
    """Deterministic sink used only by the local regression endpoint."""

    def __init__(
        self,
        outcomes: list[DeliveryErrorClass | None],
        *,
        block_first: bool = False,
    ) -> None:
        self._outcomes = outcomes
        self._block_first = block_first
        self._first_started = asyncio.Event()
        self._release_first = asyncio.Event()
        self.calls = 0

    async def deliver(self, _record: Any) -> None:
        self.calls += 1
        if self._block_first and self.calls == 1:
            self._first_started.set()
            await self._release_first.wait()
        outcome = self._outcomes[self.calls - 1] if self.calls <= len(self._outcomes) else None
        if outcome is not None:
            raise SinkFailure(outcome)

    async def wait_for_first(self) -> None:
        await self._first_started.wait()

    def release_first(self) -> None:
        self._release_first.set()


trace = JsonTrace()
app = FastAPI()


def _test_mode(env: Any) -> bool:
    return str(getattr(env, "SPIKE_TEST_MODE", "false")).lower() == "true"


def _event_id_from_message(message: Any) -> UUID:
    body = message.body
    try:
        value = body.event_id
    except AttributeError:
        value = body["event_id"]
    return UUID(str(value))


@app.exception_handler(PersistenceUnavailable)
async def persistence_unavailable_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Event storage unavailable"},
    )


@app.exception_handler(IdempotencyConflict)
async def idempotency_conflict_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "Idempotency-Key conflicts with an existing request"},
    )


@app.get("/health/live", include_in_schema=False)
async def liveness() -> JSONResponse:
    return JSONResponse(status_code=200, content={"status": "live"})


@app.get("/health/ready", include_in_schema=False)
async def readiness(request: Request) -> JSONResponse:
    env = request.scope["env"]
    try:
        await env.DB.prepare("SELECT 1").first()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(status_code=200, content={"status": "ready"})


@app.post("/v1/events", response_model=EventAccepted, status_code=202)
async def accept_event(
    event: EventRequest,
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=256),
    ] = None,
) -> EventAccepted:
    env = request.scope["env"]
    fault = request.headers.get("X-Hooklane-Spike-Fault") if _test_mode(env) else None
    if fault == "d1_persistence":
        raise PersistenceUnavailable
    store = D1EventStore(
        env.DB,
        fail_dispatch_transition=fault == "dispatch_transition",
        fail_payload_chunk=fault == "payload_chunk_persistence",
    )
    queue = CloudflareQueueProducer(env.DELIVERY_QUEUE, fail_send=fault == "queue_send")
    record = await AcceptanceService(store, queue, trace).accept(event, idempotency_key)
    return EventAccepted(event_id=record.event_id, status=EventStatus.QUEUED)


@app.get("/v1/events/{event_id}", response_model=EventStatusResponse)
async def event_status(event_id: UUID, request: Request) -> EventStatusResponse:
    store = D1EventStore(request.scope["env"].DB)
    record = await store.get_status(event_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return EventStatusResponse(
        event_id=record.event_id,
        status=record.status,
        attempt_count=record.attempt_count,
    )


@app.post("/__spike/repair", include_in_schema=False)
async def repair_outbox(request: Request) -> JSONResponse:
    env = request.scope["env"]
    if not _test_mode(env):
        raise HTTPException(status_code=404, detail="Not found")
    service = AcceptanceService(
        D1EventStore(env.DB),
        CloudflareQueueProducer(env.DELIVERY_QUEUE),
        trace,
    )
    repaired, deferred = await service.repair()
    return JSONResponse(content={"deferred": deferred, "repaired": repaired})


@app.get("/__spike/outbox/{event_id}", include_in_schema=False)
async def outbox_status(event_id: UUID, request: Request) -> JSONResponse:
    env = request.scope["env"]
    if not _test_mode(env):
        raise HTTPException(status_code=404, detail="Not found")
    outbox = await D1EventStore(env.DB).get_outbox_state(event_id)
    if outbox is None:
        raise HTTPException(status_code=404, detail="Not found")
    state, send_attempt_count = outbox
    return JSONResponse(content={"send_attempt_count": send_attempt_count, "state": state})


async def _expire_delivery_lease(database: Any, event_id: UUID) -> None:
    try:
        await database.prepare(
            "UPDATE events SET delivery_lease_until_ms = 0 WHERE event_id = ?1"
        ).bind(str(event_id)).run()
    except Exception:
        raise PersistenceUnavailable from None


async def _new_regression_event(store: D1EventStore) -> UUID:
    event = EventRequest(
        event_type="delivery.regression",
        payload={"message": "local-regression"},
    )
    record = await store.accept(uuid4(), event, None, request_fingerprint(event))
    if not await store.mark_dispatched(record.event_id):
        raise PersistenceUnavailable
    return record.event_id


async def _regression_snapshot(store: D1EventStore, event_id: UUID) -> dict[str, Any]:
    record = await store.get_status(event_id)
    if record is None:
        raise EventNotFound
    return {
        "attempt_count": record.attempt_count,
        "event_id": str(event_id),
        "status": record.status.value,
    }


async def _run_stale_race(
    store: D1EventStore,
    database: Any,
    event_id: UUID,
    first_outcome: DeliveryErrorClass | None,
    second_outcome: DeliveryErrorClass | None,
) -> tuple[dict[str, Any], int, str, str]:
    sink = RegressionSink([first_outcome, second_outcome], block_first=True)
    service = DeliveryService(store, sink, maximum_attempts=5, trace=trace)
    stale_task = asyncio.create_task(service.process(event_id))
    await sink.wait_for_first()
    await _expire_delivery_lease(database, event_id)
    newer_result = await service.process(event_id)
    sink.release_first()
    stale_result = await stale_task
    snapshot = await _regression_snapshot(store, event_id)
    return snapshot, sink.calls, newer_result.status.value, stale_result.status.value


@app.post("/__spike/delivery-regression", include_in_schema=False)
async def delivery_regression(request: Request) -> JSONResponse:
    env = request.scope["env"]
    if not _test_mode(env):
        raise HTTPException(status_code=404, detail="Not found")
    scenario = DeliveryRegressionRequest.model_validate(await request.json()).scenario
    store = D1EventStore(env.DB)
    event_id = await _new_regression_event(store)

    if scenario == "dead_letter_redelivery":
        sink = RegressionSink([DeliveryErrorClass.HTTP_5XX] * 5)
        service = DeliveryService(store, sink, maximum_attempts=5, trace=trace)
        for _ in range(5):
            await service.process(event_id)
        result = await service.process(event_id)
        snapshot = await _regression_snapshot(store, event_id)
        snapshot.update({"result": result.status.value, "sink_calls": sink.calls})
        return JSONResponse(content=snapshot)

    if scenario == "delivered_redelivery":
        sink = RegressionSink([None])
        service = DeliveryService(store, sink, maximum_attempts=5, trace=trace)
        await service.process(event_id)
        result = await service.process(event_id)
        snapshot = await _regression_snapshot(store, event_id)
        snapshot.update({"result": result.status.value, "sink_calls": sink.calls})
        return JSONResponse(content=snapshot)

    if scenario == "concurrent_stale_failure":
        snapshot, calls, newer_status, stale_status = await _run_stale_race(
            store,
            env.DB,
            event_id,
            DeliveryErrorClass.HTTP_5XX,
            None,
        )
        snapshot.update(
            {"newer_result": newer_status, "sink_calls": calls, "stale_result": stale_status}
        )
        return JSONResponse(content=snapshot)

    if scenario == "stale_success":
        snapshot, calls, newer_status, stale_status = await _run_stale_race(
            store,
            env.DB,
            event_id,
            None,
            DeliveryErrorClass.HTTP_5XX,
        )
        snapshot.update(
            {"newer_result": newer_status, "sink_calls": calls, "stale_result": stale_status}
        )
        return JSONResponse(content=snapshot)

    store = D1EventStore(env.DB, fail_delivery_transition=True)
    sink = RegressionSink([None, None])
    service = DeliveryService(store, sink, maximum_attempts=5, trace=trace)
    try:
        await service.process(event_id)
    except PersistenceUnavailable:
        transition_failed = True
    else:
        transition_failed = False
    await _expire_delivery_lease(env.DB, event_id)
    result = await service.process(event_id)
    snapshot = await _regression_snapshot(store, event_id)
    snapshot.update(
        {
            "result": result.status.value,
            "sink_calls": sink.calls,
            "transition_failed": transition_failed,
        }
    )
    return JSONResponse(content=snapshot)


class Default(WorkerEntrypoint):
    async def fetch(self, request: Any) -> Any:
        return await asgi.fetch(app, request, self.env)

    async def queue(self, batch: Any, _env: Any, _ctx: Any) -> None:
        store = D1EventStore(self.env.DB)
        sink = FixedMockSink(str(self.env.MOCK_SINK_URL))
        service = DeliveryService(store, sink, trace=trace)
        for message in batch.messages:
            try:
                event_id = _event_id_from_message(message)
                result = await service.process(event_id)
            except (PersistenceUnavailable, EventNotFound):
                message.retry()
                continue
            if result.disposition is DeliveryDisposition.ACK:
                message.ack()
            else:
                message.retry({"delaySeconds": result.retry_delay_seconds})

    async def scheduled(self, _controller: Any, _env: Any, _ctx: Any) -> None:
        service = AcceptanceService(
            D1EventStore(self.env.DB),
            CloudflareQueueProducer(self.env.DELIVERY_QUEUE),
            trace,
        )
        await service.repair()
