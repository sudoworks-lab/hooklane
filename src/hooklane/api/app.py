"""FastAPI application for accepting events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
import os
from time import perf_counter
from typing import Annotated, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.requests import Request

from hooklane.domain.events import EventAccepted, EventRequest, EventStatus, EventStatusResponse
from hooklane.observability.logging import LogEvent, LogLevel, StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics
from hooklane.queue.events import (
    EventStore,
    EventStoreUnavailable,
    IdempotencyConflict,
    RedisEventStore,
)
from hooklane.runtime import ServiceHealth


DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
REQUEST_ID: ContextVar[str | None] = ContextVar("hooklane_request_id", default=None)


@runtime_checkable
class QueueMetricsStore(Protocol):
    """Optional queue snapshot boundary used only while rendering metrics."""

    async def refresh_queue_metrics(self, group_name: str = "hooklane-workers") -> bool: ...


def current_request_id() -> str | None:
    return REQUEST_ID.get()


def _http_reason_code(status_code: int) -> str:
    if status_code < 400:
        return "none"
    if status_code in {400, 422}:
        return "validation_error"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "idempotency_conflict"
    if status_code == 429:
        return "http_429"
    if status_code == 503:
        return "storage_unavailable"
    if status_code < 500:
        return "http_4xx"
    if status_code == 500:
        return "internal_error"
    return "http_5xx"


def _request_observability(request: Request) -> tuple[HooklaneMetrics, StructuredLogger]:
    return request.app.state.metrics, request.app.state.structured_logger


async def validation_error_response(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Return validation details without reflecting request input."""

    validation_error = cast(RequestValidationError, exc)
    invalid_json = any(error["type"] == "json_invalid" for error in validation_error.errors())
    _metrics, logger = _request_observability(request)
    logger.emit(
        LogEvent.REQUEST_REJECTED,
        level=LogLevel.WARNING,
        request_id=current_request_id(),
        status=400 if invalid_json else 422,
        outcome="rejected",
        reason_code="validation_error",
    )
    if invalid_json:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON request body"})
    return JSONResponse(status_code=422, content={"detail": "Request validation failed"})


async def event_store_error_response(
    request: Request,
    _exc: Exception,
) -> JSONResponse:
    """Return a fixed response without exposing Redis connection details."""

    _metrics, logger = _request_observability(request)
    logger.emit(
        LogEvent.REQUEST_REJECTED,
        level=LogLevel.ERROR,
        request_id=current_request_id(),
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
        outcome="failure",
        reason_code="storage_unavailable",
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Event storage unavailable"},
    )


async def idempotency_conflict_response(
    request: Request,
    _exc: Exception,
) -> JSONResponse:
    """Return a fixed conflict without reflecting the key or request content."""

    _metrics, logger = _request_observability(request)
    logger.emit(
        LogEvent.REQUEST_REJECTED,
        level=LogLevel.WARNING,
        request_id=current_request_id(),
        status=status.HTTP_409_CONFLICT,
        outcome="rejected",
        reason_code="idempotency_conflict",
    )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "Idempotency-Key conflicts with an existing request"},
    )


async def always_ready() -> bool:
    return True


def create_app(
    *,
    event_store: EventStore | None = None,
    health: ServiceHealth | None = None,
    readiness_probe: Callable[[], Awaitable[bool]] | None = None,
    shutdown_grace_seconds: float = 10.0,
    metrics: HooklaneMetrics | None = None,
    logger: StructuredLogger | None = None,
) -> FastAPI:
    """Create an isolated application instance."""

    app_metrics = metrics or HooklaneMetrics("api")
    app_logger = logger or StructuredLogger("api")
    owned_store = (
        RedisEventStore.from_url(
            os.environ.get("HOOKLANE_REDIS_URL", DEFAULT_REDIS_URL),
            metrics=app_metrics,
            logger=app_logger,
        )
        if event_store is None
        else None
    )
    store = event_store if event_store is not None else owned_store
    assert store is not None
    service_health = health or ServiceHealth(
        started=event_store is not None,
        dependency_ready=event_store is not None,
    )
    probe = readiness_probe
    if probe is None:
        probe = owned_store.ping if owned_store is not None else always_ready

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        service_health.mark_started(dependency_ready=await probe())
        try:
            yield
        finally:
            service_health.begin_shutdown()
            await service_health.wait_for_drain(shutdown_grace_seconds)
            if owned_store is not None:
                await owned_store.close()

    application = FastAPI(lifespan=lifespan)
    application.state.health = service_health
    application.state.metrics = app_metrics
    application.state.structured_logger = app_logger
    application.add_exception_handler(RequestValidationError, validation_error_response)
    application.add_exception_handler(EventStoreUnavailable, event_store_error_response)
    application.add_exception_handler(IdempotencyConflict, idempotency_conflict_response)

    @application.middleware("http")
    async def reject_new_events_during_shutdown(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method != "POST" or request.url.path != "/v1/events":
            return await call_next(request)
        if not service_health.begin_work():
            reason_code = (
                "shutting_down" if service_health.shutting_down else "storage_unavailable"
            )
            if reason_code == "storage_unavailable":
                app_metrics.record_enqueue("failure", reason_code)
            app_logger.emit(
                LogEvent.REQUEST_REJECTED,
                level=LogLevel.WARNING,
                request_id=current_request_id(),
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
                outcome="rejected",
                reason_code=reason_code,
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Event acceptance unavailable"},
            )
        try:
            return await call_next(request)
        finally:
            service_health.finish_work()

    @application.middleware("http")
    async def observe_http_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = str(uuid4())
        token = REQUEST_ID.set(request_id)
        started_at = perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                app_logger.emit(
                    LogEvent.REQUEST_REJECTED,
                    level=LogLevel.ERROR,
                    request_id=request_id,
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    outcome="failure",
                    reason_code="internal_error",
                )
                response = JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={"detail": "Internal server error"},
                )
            duration_seconds = perf_counter() - started_at
            route = getattr(request.scope.get("route"), "path", None)
            app_metrics.record_http(
                request.method,
                route,
                response.status_code,
                duration_seconds,
            )
            app_logger.emit(
                LogEvent.HTTP_REQUEST_COMPLETED,
                request_id=request_id,
                status=response.status_code,
                outcome="success" if response.status_code < 400 else "failure",
                reason_code=_http_reason_code(response.status_code),
                duration_ms=duration_seconds * 1000,
            )
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            REQUEST_ID.reset(token)

    @application.get("/health/live", include_in_schema=False)
    async def liveness() -> JSONResponse:
        return JSONResponse(status_code=200, content={"status": "live"})

    @application.get("/health/ready", include_in_schema=False)
    async def readiness() -> JSONResponse:
        if service_health.started and not service_health.shutting_down:
            service_health.set_dependency_ready(await probe())
        if service_health.ready:
            app_metrics.set_ready(True)
            return JSONResponse(status_code=200, content={"status": "ready"})
        app_metrics.set_ready(False)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready"},
        )

    @application.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        if isinstance(store, QueueMetricsStore):
            await store.refresh_queue_metrics()
        app_metrics.set_ready(service_health.ready)
        return Response(
            content=app_metrics.render(),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    @application.post(
        "/v1/events",
        response_model=EventAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def accept_event(
        event: EventRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=256),
        ] = None,
    ) -> EventAccepted:
        event_id = uuid4()
        try:
            if idempotency_key is None:
                await store.enqueue(event_id, event)
            else:
                event_id = await store.enqueue_idempotent(event_id, event, idempotency_key)
        except IdempotencyConflict:
            app_metrics.record_enqueue("failure", "idempotency_conflict")
            raise
        except EventStoreUnavailable:
            app_metrics.record_enqueue("failure", "storage_unavailable")
            raise
        app_metrics.record_enqueue("success")
        app_logger.emit(
            LogEvent.EVENT_ACCEPTED,
            request_id=current_request_id(),
            event_id=event_id,
            attempt=0,
            status=EventStatus.QUEUED.value,
            outcome="success",
            reason_code="none",
        )
        return EventAccepted(event_id=event_id, status=EventStatus.QUEUED)

    @application.get(
        "/v1/events/{event_id}",
        response_model=EventStatusResponse,
    )
    async def get_event_status(event_id: UUID) -> EventStatusResponse:
        event_status = await store.get_status(event_id)
        if event_status is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found",
            )
        app_logger.emit(
            LogEvent.EVENT_STATUS_READ,
            request_id=current_request_id(),
            event_id=event_status.event_id,
            attempt=event_status.attempt_count,
            status=event_status.status.value,
            outcome="success",
            reason_code="none",
        )
        return EventStatusResponse(
            event_id=event_status.event_id,
            status=event_status.status,
            attempt_count=event_status.attempt_count,
        )

    return application


app = create_app()
