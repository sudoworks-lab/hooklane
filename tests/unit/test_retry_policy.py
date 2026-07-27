from __future__ import annotations

from random import Random
from uuid import uuid4

import pytest
from httpx import ConnectError, MockTransport, ReadTimeout, Request, Response

from hooklane.delivery.retry import DeliveryErrorClass, RetryPolicy
from hooklane.delivery.sink import DeliveryFailed, MockSinkClient
from hooklane.domain.events import EventRequest
from hooklane.queue.events import QueuedEvent


def queued_event() -> QueuedEvent:
    return QueuedEvent(
        stream_id="1-0",
        event_id=uuid4(),
        event=EventRequest(event_type="delivery.test", payload={"message": "retry"}),
    )


@pytest.mark.parametrize(
    "error_class",
    [
        DeliveryErrorClass.TIMEOUT,
        DeliveryErrorClass.CONNECTION,
        DeliveryErrorClass.HTTP_429,
        DeliveryErrorClass.HTTP_5XX,
    ],
)
def test_retryable_delivery_error_classes(error_class: DeliveryErrorClass) -> None:
    assert RetryPolicy.is_retryable(error_class)


def test_other_http_4xx_is_not_retryable() -> None:
    assert not RetryPolicy.is_retryable(DeliveryErrorClass.HTTP_4XX)


def test_http_3xx_and_1xx_are_not_retryable() -> None:
    assert not RetryPolicy.is_retryable(DeliveryErrorClass.HTTP_1XX)
    assert not RetryPolicy.is_retryable(DeliveryErrorClass.HTTP_3XX)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (200, None),
        (202, None),
        (204, None),
        (301, DeliveryErrorClass.HTTP_3XX),
        (302, DeliveryErrorClass.HTTP_3XX),
        (307, DeliveryErrorClass.HTTP_3XX),
        (308, DeliveryErrorClass.HTTP_3XX),
        (429, DeliveryErrorClass.HTTP_429),
        (500, DeliveryErrorClass.HTTP_5XX),
        (503, DeliveryErrorClass.HTTP_5XX),
        (400, DeliveryErrorClass.HTTP_4XX),
    ],
)
async def test_sink_classifies_http_failures(
    status_code: int,
    expected_error: DeliveryErrorClass | None,
) -> None:
    marker = "secret-like-payload"
    event = QueuedEvent(
        stream_id="1-0",
        event_id=uuid4(),
        event=EventRequest(event_type="delivery.test", payload={"marker": marker}),
    )

    def respond(request: Request) -> Response:
        return Response(
            status_code,
            headers={"Location": f"https://redirect.invalid/{marker}"},
            request=request,
        )

    client = MockSinkClient(transport=MockTransport(respond))

    try:
        if expected_error is None:
            await client.deliver(event)
        else:
            with pytest.raises(DeliveryFailed) as failure:
                await client.deliver(event)
    finally:
        await client.close()

    if expected_error is not None:
        assert failure.value.error_class is expected_error
        assert marker not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "expected_error"),
    [
        (ReadTimeout, DeliveryErrorClass.TIMEOUT),
        (ConnectError, DeliveryErrorClass.CONNECTION),
    ],
)
async def test_sink_classifies_transport_failures(
    exception_type: type[ReadTimeout] | type[ConnectError],
    expected_error: DeliveryErrorClass,
) -> None:
    def fail(request: Request) -> Response:
        raise exception_type("delivery failed", request=request)

    client = MockSinkClient(transport=MockTransport(fail))

    try:
        with pytest.raises(DeliveryFailed) as failure:
            await client.deliver(queued_event())
    finally:
        await client.close()

    assert failure.value.error_class is expected_error


def test_backoff_is_exponential_and_capped_without_jitter() -> None:
    policy = RetryPolicy(
        base_delay_seconds=1,
        maximum_delay_seconds=8,
        jitter_ratio=0,
    )
    random_source = Random(7)

    delays = [policy.delay_seconds(attempt, random_source) for attempt in range(1, 6)]

    assert delays == [1, 2, 4, 8, 8]


def test_seeded_jitter_stays_within_policy_bounds() -> None:
    policy = RetryPolicy(
        base_delay_seconds=4,
        maximum_delay_seconds=100,
        jitter_ratio=0.25,
    )
    random_source = Random(7)

    delays = [policy.delay_seconds(2, random_source) for _ in range(20)]

    assert all(6 <= delay <= 10 for delay in delays)
    assert len(set(delays)) > 1
