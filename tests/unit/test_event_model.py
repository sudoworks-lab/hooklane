from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from hooklane.domain.events import EventAccepted, EventRequest, EventStatus


def test_event_request_accepts_json_object_payload() -> None:
    event = EventRequest(event_type="delivery.test", payload={})

    assert event.event_type == "delivery.test"
    assert event.payload == {}


@pytest.mark.parametrize("event_type", ["", "   "])
def test_event_request_rejects_blank_event_type(event_type: str) -> None:
    with pytest.raises(ValidationError):
        EventRequest(event_type=event_type, payload={})


def test_event_request_rejects_non_object_payload() -> None:
    with pytest.raises(ValidationError):
        EventRequest.model_validate({"event_type": "delivery.test", "payload": []})


def test_event_accepted_starts_queued() -> None:
    event_id = uuid4()

    accepted = EventAccepted(event_id=event_id)

    assert accepted.event_id == event_id
    assert accepted.status is EventStatus.QUEUED
