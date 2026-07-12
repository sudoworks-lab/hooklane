from __future__ import annotations

import asyncio
import signal

import pytest

from hooklane.runtime import ServiceHealth, ShutdownCoordinator


class SignalLoop:
    def __init__(self) -> None:
        self.handlers: dict[signal.Signals, object] = {}

    def add_signal_handler(
        self,
        sig: signal.Signals,
        callback: object,
    ) -> None:
        self.handlers[sig] = callback


@pytest.mark.asyncio
async def test_shutdown_rejects_new_work_and_drains_in_flight_work() -> None:
    health = ServiceHealth(started=True, dependency_ready=True)
    assert health.begin_work()

    health.begin_shutdown()
    assert not health.ready
    assert not health.begin_work()

    async def finish() -> None:
        await asyncio.sleep(0)
        health.finish_work()

    finish_task = asyncio.create_task(finish())
    assert await health.wait_for_drain(timeout_seconds=1)
    await finish_task


@pytest.mark.asyncio
async def test_sigterm_requests_shutdown_without_timing_sleep() -> None:
    health = ServiceHealth(started=True, dependency_ready=True)
    coordinator = ShutdownCoordinator(on_shutdown=health.begin_shutdown)
    loop = SignalLoop()
    coordinator.install(loop)

    handler = loop.handlers[signal.SIGTERM]
    assert callable(handler)
    handler()

    assert coordinator.requested
    assert not health.ready
    await coordinator.wait()
