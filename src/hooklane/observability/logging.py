"""Bounded structured JSON logging shared by Hooklane services."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
import json
import sys
from typing import Any
from uuid import UUID


class LogLevel(StrEnum):
    """Levels emitted by the public application log contract."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogEvent(StrEnum):
    """Finite event names prevent user input from becoming log structure."""

    HTTP_REQUEST_COMPLETED = "http_request_completed"
    REQUEST_REJECTED = "request_rejected"
    EVENT_ACCEPTED = "event_accepted"
    EVENT_STATUS_READ = "event_status_read"
    REDIS_OPERATION_FAILED = "redis_operation_failed"
    DELIVERY_STARTED = "delivery_started"
    DELIVERY_COMPLETED = "delivery_completed"
    DELIVERY_FAILED = "delivery_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    DELIVERY_RECEIVED = "delivery_received"
    WORKER_STARTED = "worker_started"
    WORKER_STOPPED = "worker_stopped"


SERVICE_NAMES = frozenset({"api", "worker", "mock_sink"})
OUTCOMES = frozenset(
    {
        "success",
        "failure",
        "rejected",
        "retry_scheduled",
        "dead_lettered",
        "pending",
    }
)
REASON_CODES = frozenset(
    {
        "none",
        "validation_error",
        "storage_unavailable",
        "idempotency_conflict",
        "not_found",
        "shutting_down",
        "internal_error",
        "redis_error",
        "timeout",
        "connection_error",
        "http_429",
        "http_5xx",
        "http_4xx",
    }
)
EVENT_STATUSES = frozenset(
    {"queued", "delivering", "retry_scheduled", "delivered", "dead_letter"}
)
LOG_FIELDS = frozenset(
    {
        "timestamp",
        "level",
        "service",
        "event",
        "request_id",
        "event_id",
        "attempt",
        "status",
        "outcome",
        "reason_code",
        "duration_ms",
    }
)
FORBIDDEN_LOG_FIELDS = frozenset(
    {
        "payload",
        "idempotency_key",
        "credential",
        "redis_password",
        "redis_url",
        "cookie",
        "exception",
        "stack_trace",
    }
)


def _stdout_sink(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _uuid_text(value: UUID | str | None, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise ValueError(f"{field_name} must be a UUID") from None


def _status_text(value: int | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        if not 100 <= value <= 599:
            raise ValueError("HTTP status must be between 100 and 599")
        return str(value)
    if value not in EVENT_STATUSES:
        raise ValueError("status must be a bounded event or HTTP status")
    return value


class StructuredLogger:
    """Emit one JSON object per line without accepting arbitrary fields or messages."""

    def __init__(
        self,
        service: str,
        *,
        sink: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if service not in SERVICE_NAMES:
            raise ValueError("unknown service")
        self.service = service
        self._sink = sink or _stdout_sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def emit(
        self,
        event: LogEvent,
        *,
        level: LogLevel = LogLevel.INFO,
        request_id: UUID | str | None = None,
        event_id: UUID | str | None = None,
        attempt: int | None = None,
        status: int | str | None = None,
        outcome: str | None = None,
        reason_code: str | None = None,
        duration_ms: float | None = None,
    ) -> dict[str, Any]:
        """Validate and emit the common contract, returning it for deterministic tests."""

        if attempt is not None and attempt < 0:
            raise ValueError("attempt must not be negative")
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("duration must not be negative")
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError("unknown outcome")
        if reason_code is not None and reason_code not in REASON_CODES:
            raise ValueError("unknown reason code")

        timestamp = self._clock().astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        record: dict[str, Any] = {
            "timestamp": timestamp,
            "level": level.value,
            "service": self.service,
            "event": event.value,
        }
        optional: dict[str, Any] = {
            "request_id": _uuid_text(request_id, "request_id"),
            "event_id": _uuid_text(event_id, "event_id"),
            "attempt": attempt,
            "status": _status_text(status),
            "outcome": outcome,
            "reason_code": reason_code,
            "duration_ms": round(duration_ms, 3) if duration_ms is not None else None,
        }
        record.update({key: value for key, value in optional.items() if value is not None})
        self._sink(json.dumps(record, separators=(",", ":"), sort_keys=True))
        return record
