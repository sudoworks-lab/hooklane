"""Deterministic internal sink used by local delivery verification."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
import os
from time import perf_counter
from uuid import UUID
from uuid import uuid4

from fastapi import FastAPI, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict, JsonValue
from starlette.requests import Request

from hooklane.observability.logging import LogEvent, LogLevel, StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics


class MockSinkMode(StrEnum):
    """Deterministic responses supported by the project-owned sink."""

    ACCEPT = "accept"
    DELAY = "delay"
    POST_RECEIPT_DELAY = "post_receipt_delay"
    SERVER_ERROR = "server_error"


class DeliveryRequest(BaseModel):
    """Internal delivery contract between the worker and mock sink."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str
    payload: dict[str, JsonValue]


class MockSinkReceipts:
    """Keep event-ID-only receipts and deduplicate repeat deliveries."""

    def __init__(self) -> None:
        self._event_ids: set[UUID] = set()

    def record(self, event_id: UUID) -> None:
        self._event_ids.add(event_id)

    @property
    def event_ids(self) -> frozenset[UUID]:
        return frozenset(self._event_ids)


def _http_reason_code(status_code: int) -> str:
    if status_code < 400:
        return "none"
    if status_code == 429:
        return "http_429"
    if status_code < 500:
        return "http_4xx"
    if status_code == 500:
        return "internal_error"
    return "http_5xx"


def create_app(
    *,
    receipts: MockSinkReceipts,
    mode: MockSinkMode = MockSinkMode.ACCEPT,
    delay_seconds: float = 0.0,
    metrics: HooklaneMetrics | None = None,
    logger: StructuredLogger | None = None,
) -> FastAPI:
    """Create a sink that either accepts or deterministically returns 503."""

    if delay_seconds < 0:
        raise ValueError("delay must not be negative")

    app_metrics = metrics or HooklaneMetrics("mock_sink")
    app_logger = logger or StructuredLogger("mock_sink")
    application = FastAPI()
    application.state.metrics = app_metrics
    application.state.structured_logger = app_logger

    @application.middleware("http")
    async def observe_http_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = str(uuid4())
        started_at = perf_counter()
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
            response = Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
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

    @application.get("/health/live", include_in_schema=False)
    async def liveness() -> Response:
        app_metrics.set_ready(True)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        app_metrics.set_ready(True)
        return Response(
            content=app_metrics.render(),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    @application.post("/internal/deliveries", status_code=status.HTTP_204_NO_CONTENT)
    async def accept_delivery(delivery: DeliveryRequest) -> Response:
        if mode is MockSinkMode.SERVER_ERROR:
            app_logger.emit(
                LogEvent.DELIVERY_RECEIVED,
                level=LogLevel.WARNING,
                event_id=delivery.event_id,
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
                outcome="failure",
                reason_code="http_5xx",
            )
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        if mode is MockSinkMode.POST_RECEIPT_DELAY:
            receipts.record(delivery.event_id)
            app_logger.emit(
                LogEvent.DELIVERY_RECEIVED,
                event_id=delivery.event_id,
                status=status.HTTP_204_NO_CONTENT,
                outcome="success",
                reason_code="none",
            )
            await asyncio.sleep(delay_seconds)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if mode is MockSinkMode.DELAY:
            await asyncio.sleep(delay_seconds)
        receipts.record(delivery.event_id)
        app_logger.emit(
            LogEvent.DELIVERY_RECEIVED,
            event_id=delivery.event_id,
            status=status.HTTP_204_NO_CONTENT,
            outcome="success",
            reason_code="none",
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return application


app = create_app(
    receipts=MockSinkReceipts(),
    mode=MockSinkMode(os.environ.get("HOOKLANE_MOCK_SINK_MODE", MockSinkMode.ACCEPT.value)),
    delay_seconds=float(os.environ.get("HOOKLANE_MOCK_SINK_DELAY_SECONDS", "0")),
)
