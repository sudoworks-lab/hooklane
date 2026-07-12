from __future__ import annotations

import pytest

from hooklane.worker.main import WorkerSettings, load_worker_settings


def test_worker_settings_use_safe_defaults() -> None:
    assert load_worker_settings({}) == WorkerSettings(
        maximum_attempts=5,
        pending_idle_ms=60_000,
    )


def test_worker_settings_read_helm_environment() -> None:
    assert load_worker_settings(
        {
            "HOOKLANE_RETRY_MAXIMUM_ATTEMPTS": "20",
            "HOOKLANE_PENDING_IDLE_MILLISECONDS": "1000",
        }
    ) == WorkerSettings(maximum_attempts=20, pending_idle_ms=1000)


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"HOOKLANE_RETRY_MAXIMUM_ATTEMPTS": "0"}, "maximum attempts"),
        ({"HOOKLANE_PENDING_IDLE_MILLISECONDS": "-1"}, "pending idle"),
    ],
)
def test_worker_settings_reject_invalid_bounds(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        load_worker_settings(environment)
