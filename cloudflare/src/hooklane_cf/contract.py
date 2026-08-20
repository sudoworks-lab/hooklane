"""Public models and canonicalization shared by the Cloudflare spike."""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


QUEUE_MESSAGE_LIMIT_BYTES = 128 * 1024
D1_ROW_LIMIT_BYTES = 2_000_000
PAYLOAD_CHUNK_LIMIT_BYTES = 1_500_000


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
    """Public response returned after durable D1 acceptance."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: UUID
    status: EventStatus = EventStatus.QUEUED


class EventStatusResponse(BaseModel):
    """Public delivery state for a D1-persisted event."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: UUID
    status: EventStatus
    attempt_count: int = Field(ge=0)


def canonical_request(event: EventRequest) -> str:
    """Return the stable representation used for idempotency comparison."""

    return json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def request_fingerprint(event: EventRequest) -> str:
    """Hash request content without storing it in the idempotency index."""

    return sha256(canonical_request(event).encode("utf-8")).hexdigest()


def payload_json_chunks(event: EventRequest) -> tuple[str, ...]:
    """Split canonical payload JSON below D1's per-string and per-row limit."""

    encoded = json.dumps(
        event.payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    chunks: list[str] = []
    start = 0
    while start < len(encoded):
        end = min(start + PAYLOAD_CHUNK_LIMIT_BYTES, len(encoded))
        if end < len(encoded):
            while end > start and encoded[end] & 0b1100_0000 == 0b1000_0000:
                end -= 1
        if end == start:
            raise ValueError("payload chunk boundary is not valid UTF-8")
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
    return tuple(chunks)


def idempotency_key_hash(idempotency_key: str | None) -> str | None:
    """Hash the caller-provided key so its raw value is never persisted."""

    if idempotency_key is None:
        return None
    return sha256(idempotency_key.encode("utf-8")).hexdigest()


def queue_reference_size(event_id: UUID | str) -> int:
    """Return the UTF-8 JSON size of the actual Queue message."""

    return len(
        json.dumps(
            {"event_id": str(event_id)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def direct_queue_envelope_size(event_id: UUID | str, event: EventRequest) -> int:
    """Return the direct-payload envelope size used only for limit evidence."""

    envelope = {
        "event_id": str(event_id),
        "event_type": event.event_type,
        "payload": event.payload,
    }
    return len(
        json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
