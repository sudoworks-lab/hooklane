"""Shared service health and deterministic shutdown coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import signal
from typing import Protocol


class SignalRegistrar(Protocol):
    def add_signal_handler(
        self,
        sig: signal.Signals,
        callback: Callable[[], None],
    ) -> None: ...


class ServiceHealth:
    """Keep startup, readiness, liveness, and shutdown state separate."""

    def __init__(
        self,
        *,
        started: bool = False,
        dependency_ready: bool = False,
    ) -> None:
        self._started = started
        self._dependency_ready = dependency_ready
        self._shutting_down = False
        self._in_flight = 0
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def live(self) -> bool:
        return True

    @property
    def started(self) -> bool:
        return self._started

    @property
    def ready(self) -> bool:
        return self._started and self._dependency_ready and not self._shutting_down

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def mark_started(self, dependency_ready: bool) -> None:
        self._started = True
        self._dependency_ready = dependency_ready

    def set_dependency_ready(self, ready: bool) -> None:
        self._dependency_ready = ready

    def begin_work(self) -> bool:
        if not self.ready:
            return False
        self._in_flight += 1
        self._drained.clear()
        return True

    def finish_work(self) -> None:
        if self._in_flight < 1:
            raise RuntimeError("no in-flight work to finish")
        self._in_flight -= 1
        if self._in_flight == 0:
            self._drained.set()

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    async def wait_for_drain(self, timeout_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return True


class ShutdownCoordinator:
    """Convert SIGINT or SIGTERM into an awaitable shutdown request."""

    def __init__(self, *, on_shutdown: Callable[[], None] | None = None) -> None:
        self._event = asyncio.Event()
        self._on_shutdown = on_shutdown

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self) -> None:
        if self._event.is_set():
            return
        if self._on_shutdown is not None:
            self._on_shutdown()
        self._event.set()

    def install(self, loop: SignalRegistrar) -> None:
        loop.add_signal_handler(signal.SIGINT, self.request)
        loop.add_signal_handler(signal.SIGTERM, self.request)

    async def wait(self) -> None:
        await self._event.wait()
