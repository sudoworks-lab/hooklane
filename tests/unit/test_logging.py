from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from hooklane.observability.logging import (
    FORBIDDEN_LOG_FIELDS,
    LOG_FIELDS,
    LogEvent,
    LogLevel,
    StructuredLogger,
)


@pytest.mark.parametrize(
    ("service", "event"),
    [
        ("api", LogEvent.EVENT_ACCEPTED),
        ("worker", LogEvent.DELIVERY_COMPLETED),
        ("mock_sink", LogEvent.DELIVERY_RECEIVED),
    ],
)
def test_services_share_one_line_json_contract(service: str, event: LogEvent) -> None:
    lines: list[str] = []
    request_id = uuid4()
    event_id = uuid4()
    logger = StructuredLogger(
        service,
        sink=lines.append,
        clock=lambda: datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc),
    )

    logger.emit(
        event,
        level=LogLevel.INFO,
        request_id=request_id,
        event_id=event_id,
        attempt=1,
        status="delivered",
        outcome="success",
        reason_code="none",
        duration_ms=12.3456,
    )

    assert len(lines) == 1
    assert "\n" not in lines[0]
    record = json.loads(lines[0])
    assert set(record) <= LOG_FIELDS
    assert record == {
        "attempt": 1,
        "duration_ms": 12.346,
        "event": event.value,
        "event_id": str(event_id),
        "level": "info",
        "outcome": "success",
        "reason_code": "none",
        "request_id": str(request_id),
        "service": service,
        "status": "delivered",
        "timestamp": "2026-07-12T00:00:00.000Z",
    }


def test_log_contract_rejects_unbounded_fields_and_values() -> None:
    lines: list[str] = []
    logger = StructuredLogger("api", sink=lines.append)

    with pytest.raises(TypeError):
        logger.emit(LogEvent.REQUEST_REJECTED, **{"payload": "forbidden"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reason code"):
        logger.emit(LogEvent.REQUEST_REJECTED, reason_code="user-supplied-message")
    with pytest.raises(ValueError, match="UUID"):
        logger.emit(LogEvent.REQUEST_REJECTED, request_id="not-a-request-id")

    assert not lines
    assert LOG_FIELDS.isdisjoint(FORBIDDEN_LOG_FIELDS)


def test_arbitrary_exception_message_is_not_published() -> None:
    marker = f"exception-{uuid4()}"
    lines: list[str] = []
    logger = StructuredLogger("worker", sink=lines.append)
    _internal_exception = RuntimeError(marker)

    logger.emit(
        LogEvent.DELIVERY_FAILED,
        level=LogLevel.ERROR,
        event_id=uuid4(),
        outcome="failure",
        reason_code="internal_error",
    )

    assert marker not in lines[0]
    assert "exception" not in json.loads(lines[0])
