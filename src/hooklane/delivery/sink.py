"""Controlled delivery client with a mock-sink-compatible default."""

from __future__ import annotations

from httpx import AsyncBaseTransport, AsyncClient, ConnectError, RequestError, TimeoutException

from hooklane.delivery.retry import DeliveryErrorClass
from hooklane.queue.events import QueuedEvent
from hooklane.runtime_config import (
    downstream_config_from_environment,
    parse_downstream_url,
)


MOCK_SINK_ORIGIN = "http://hooklane-mock-sink:8080"
MOCK_SINK_PATH = "/internal/deliveries"
DELIVERY_TARGET_ALLOWLIST = frozenset({MOCK_SINK_ORIGIN})
DELIVERY_GUARANTEE = "at-least-once"
DOWNSTREAM_DEDUPLICATION_KEY = "event_id"


class DeliveryFailed(Exception):
    """Raised when the configured downstream does not accept a delivery."""

    def __init__(self, error_class: DeliveryErrorClass) -> None:
        super().__init__(error_class.value)
        self.error_class = error_class


class MockSinkClient:
    """Deliver to a controlled endpoint, defaulting to the project mock sink."""

    def __init__(
        self,
        *,
        destination_url: str | None = None,
        transport: AsyncBaseTransport | None = None,
    ) -> None:
        self._target = (
            downstream_config_from_environment()
            if destination_url is None
            else parse_downstream_url(destination_url)
        )
        self._client = AsyncClient(
            transport=transport,
            timeout=5.0,
        )

    @property
    def target_origin(self) -> str:
        """Return the configured delivery origin without credentials."""

        return self._target.origin

    @property
    def target_url(self) -> str:
        """Return the configured endpoint for deterministic non-secret tests."""

        return self._target.value

    async def deliver(self, queued_event: QueuedEvent) -> None:
        """Send an event without exposing sink errors or request content."""

        try:
            response = await self._client.post(
                self._target.value,
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
