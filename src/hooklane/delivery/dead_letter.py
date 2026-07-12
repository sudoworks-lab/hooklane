"""Policy for terminating delivery attempts in the dead-letter stream."""

from __future__ import annotations

from dataclasses import dataclass

from hooklane.delivery.retry import DeliveryErrorClass, RetryPolicy


@dataclass(frozen=True)
class DeadLetterPolicy:
    """Decide when a failed delivery must not be retried again."""

    maximum_attempts: int = 5

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1:
            raise ValueError("maximum attempts must be positive")

    def should_dead_letter(
        self,
        error_class: DeliveryErrorClass,
        attempt_count: int,
    ) -> bool:
        """Return whether the failure is terminal at the current attempt."""

        if attempt_count < 1:
            raise ValueError("attempt count must be positive")
        return (
            not RetryPolicy.is_retryable(error_class)
            or attempt_count >= self.maximum_attempts
        )
