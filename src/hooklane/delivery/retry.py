"""Deterministic retry classification and bounded backoff policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from random import Random


class DeliveryErrorClass(StrEnum):
    """Stable delivery failure classes used by retry and status persistence."""

    TIMEOUT = "timeout"
    CONNECTION = "connection_error"
    HTTP_429 = "http_429"
    HTTP_5XX = "http_5xx"
    HTTP_4XX = "http_4xx"


@dataclass(frozen=True)
class RetryPolicy:
    """Classify failures and calculate exponential backoff with bounded jitter."""

    base_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 60.0
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if self.base_delay_seconds <= 0:
            raise ValueError("base delay must be positive")
        if self.maximum_delay_seconds < self.base_delay_seconds:
            raise ValueError("maximum delay must not be below the base delay")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter ratio must be between zero and one")

    @staticmethod
    def is_retryable(error_class: DeliveryErrorClass) -> bool:
        """Return whether a delivery failure is eligible for automatic retry."""

        return error_class in {
            DeliveryErrorClass.TIMEOUT,
            DeliveryErrorClass.CONNECTION,
            DeliveryErrorClass.HTTP_429,
            DeliveryErrorClass.HTTP_5XX,
        }

    def delay_seconds(self, attempt_count: int, random_source: Random) -> float:
        """Return a capped exponential delay using an injected random source."""

        if attempt_count < 1:
            raise ValueError("attempt count must be positive")
        exponent = min(attempt_count - 1, 62)
        uncapped_delay = self.base_delay_seconds * (2**exponent)
        backoff = min(uncapped_delay, self.maximum_delay_seconds)
        jitter_multiplier = random_source.uniform(
            1 - self.jitter_ratio,
            1 + self.jitter_ratio,
        )
        return float(min(backoff * jitter_multiplier, self.maximum_delay_seconds))
