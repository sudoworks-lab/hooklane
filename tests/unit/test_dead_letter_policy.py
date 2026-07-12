from __future__ import annotations

import pytest

from hooklane.delivery.dead_letter import DeadLetterPolicy
from hooklane.delivery.retry import DeliveryErrorClass


@pytest.mark.parametrize(
    "error_class",
    [
        DeliveryErrorClass.HTTP_4XX,
    ],
)
def test_non_retryable_error_is_dead_lettered_immediately(
    error_class: DeliveryErrorClass,
) -> None:
    policy = DeadLetterPolicy(maximum_attempts=3)

    assert policy.should_dead_letter(error_class, attempt_count=1)


@pytest.mark.parametrize(
    "error_class",
    [
        DeliveryErrorClass.TIMEOUT,
        DeliveryErrorClass.CONNECTION,
        DeliveryErrorClass.HTTP_429,
        DeliveryErrorClass.HTTP_5XX,
    ],
)
def test_retryable_error_is_dead_lettered_only_at_attempt_limit(
    error_class: DeliveryErrorClass,
) -> None:
    policy = DeadLetterPolicy(maximum_attempts=3)

    assert not policy.should_dead_letter(error_class, attempt_count=2)
    assert policy.should_dead_letter(error_class, attempt_count=3)


def test_dead_letter_policy_rejects_non_positive_attempt_limit() -> None:
    with pytest.raises(ValueError, match="maximum attempts must be positive"):
        DeadLetterPolicy(maximum_attempts=0)


def test_dead_letter_policy_rejects_non_positive_attempt_count() -> None:
    policy = DeadLetterPolicy()

    with pytest.raises(ValueError, match="attempt count must be positive"):
        policy.should_dead_letter(DeliveryErrorClass.HTTP_4XX, attempt_count=0)
