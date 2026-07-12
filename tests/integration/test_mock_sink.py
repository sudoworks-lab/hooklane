from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport

from hooklane.delivery.sink import (
    DELIVERY_GUARANTEE,
    DELIVERY_TARGET_ALLOWLIST,
    DOWNSTREAM_DEDUPLICATION_KEY,
    MOCK_SINK_ORIGIN,
    DeliveryFailed,
    MockSinkClient,
)
from hooklane.domain.events import EventRequest
from hooklane.mock_sink.app import MockSinkMode, MockSinkReceipts, create_app
from hooklane.queue.events import QueuedEvent


def queued_event() -> QueuedEvent:
    return QueuedEvent(
        stream_id="1-0",
        event_id=uuid4(),
        event=EventRequest(event_type="delivery.test", payload={"message": "accepted"}),
    )


@pytest.mark.asyncio
async def test_mock_sink_deduplicates_at_least_once_delivery_by_event_id() -> None:
    receipts = MockSinkReceipts()
    event = queued_event()
    client = MockSinkClient(transport=ASGITransport(app=create_app(receipts=receipts)))

    try:
        await client.deliver(event)
        await client.deliver(event)
    finally:
        await client.close()

    assert receipts.event_ids == frozenset({event.event_id})
    assert DELIVERY_GUARANTEE == "at-least-once"
    assert DOWNSTREAM_DEDUPLICATION_KEY == "event_id"
    assert DELIVERY_TARGET_ALLOWLIST == frozenset({MOCK_SINK_ORIGIN})
    assert client.target_origin == MOCK_SINK_ORIGIN


@pytest.mark.asyncio
async def test_mock_sink_server_error_is_a_delivery_failure() -> None:
    receipts = MockSinkReceipts()
    event = queued_event()
    client = MockSinkClient(
        transport=ASGITransport(
            app=create_app(receipts=receipts, mode=MockSinkMode.SERVER_ERROR)
        )
    )

    try:
        with pytest.raises(DeliveryFailed):
            await client.deliver(event)
    finally:
        await client.close()

    assert receipts.event_ids == frozenset()


@pytest.mark.asyncio
async def test_post_receipt_delay_exposes_at_least_once_window() -> None:
    receipts = MockSinkReceipts()
    event = queued_event()
    client = MockSinkClient(
        transport=ASGITransport(
            app=create_app(
                receipts=receipts,
                mode=MockSinkMode.POST_RECEIPT_DELAY,
                delay_seconds=0.2,
            )
        )
    )

    try:
        delivery = asyncio.create_task(client.deliver(event))
        deadline = asyncio.get_running_loop().time() + 0.15
        while event.event_id not in receipts.event_ids:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("receipt was not recorded before response delay")
            await asyncio.sleep(0.01)
        assert not delivery.done()
        await delivery
        await client.deliver(event)
    finally:
        await client.close()

    assert receipts.event_ids == frozenset({event.event_id})
