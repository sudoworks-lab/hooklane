"""Event request and acceptance models."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class EventStatus(StrEnum):
    """Delivery states exposed by the HTTP API."""

    QUEUED = "queued"
    DELIVERING = "delivering"
    RETRY_SCHEDULED = "retry_scheduled"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


class EventRequest(BaseModel):
    """Validated event accepted by Hooklane."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_type: str = Field(min_length=1, max_length=128)
    payload: dict[str, JsonValue]

    @field_validator("event_type")
    @classmethod
    def event_type_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event_type must not be blank")
        return value


class EventAccepted(BaseModel):
    """Public response returned when an event is accepted."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: UUID
    status: EventStatus = EventStatus.QUEUED


class EventStatusResponse(BaseModel):
    """Public delivery state returned for an accepted event."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: UUID
    status: EventStatus
    attempt_count: int = Field(ge=0)
