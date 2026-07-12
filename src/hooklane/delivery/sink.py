"""Fixed delivery client for Hooklane's internal mock sink."""

from __future__ import annotations

from httpx import AsyncBaseTransport, AsyncClient, ConnectError, RequestError, TimeoutException

from hooklane.delivery.retry import DeliveryErrorClass
from hooklane.queue.events import QueuedEvent


MOCK_SINK_ORIGIN = "http://hooklane-mock-sink:8080"
MOCK_SINK_PATH = "/internal/deliveries"
DELIVERY_TARGET_ALLOWLIST = frozenset({MOCK_SINK_ORIGIN})
DELIVERY_GUARANTEE = "at-least-once"
DOWNSTREAM_DEDUPLICATION_KEY = "event_id"


class DeliveryFailed(Exception):
    """Raised when the fixed mock sink does not accept a delivery."""

    def __init__(self, error_class: DeliveryErrorClass) -> None:
        super().__init__(error_class.value)
        self.error_class = error_class


class MockSinkClient:
    """Deliver only to the project-owned mock sink origin."""

    def __init__(self, *, transport: AsyncBaseTransport | None = None) -> None:
        self._client = AsyncClient(
            base_url=MOCK_SINK_ORIGIN,
            transport=transport,
            timeout=5.0,
        )

    @property
    def target_origin(self) -> str:
        """Return the immutable delivery origin used by this client."""

        return MOCK_SINK_ORIGIN

    async def deliver(self, queued_event: QueuedEvent) -> None:
        """Send an event without exposing sink errors or request content."""

        try:
            response = await self._client.post(
                MOCK_SINK_PATH,
                json={
                    "event_id": str(queued_event.event_id),
                    "event_type": queued_event.event.event_type,
                    "payload": queued_event.event.payload,
                },
            )
        except TimeoutException:
            raise DeliveryFailed(DeliveryErrorClass.TIMEOUT) from None
        except ConnectError:
            raise DeliveryFailed(DeliveryErrorClass.CONNECTION) from None
        except RequestError:
            raise DeliveryFailed(DeliveryErrorClass.CONNECTION) from None

        if response.status_code == 429:
            raise DeliveryFailed(DeliveryErrorClass.HTTP_429)
        if response.status_code >= 500:
            raise DeliveryFailed(DeliveryErrorClass.HTTP_5XX)
        if response.status_code >= 400:
            raise DeliveryFailed(DeliveryErrorClass.HTTP_4XX)

    async def close(self) -> None:
        """Close the underlying HTTP client."""

        await self._client.aclose()
