"""Worker startup, readiness, and graceful shutdown boundary."""

from __future__ import annotations

from typing import Protocol

from hooklane.queue.events import EventStoreUnavailable
from hooklane.runtime import ServiceHealth
from hooklane.worker.service import WorkerResult


class RuntimeWorker(Protocol):
    async def run_once(self) -> WorkerResult: ...


class RuntimeQueue(Protocol):
    async def ensure_consumer_group(self, group_name: str) -> None: ...


class WorkerRuntime:
    """Gate worker acquisition and drain an in-flight delivery on shutdown."""

    def __init__(
        self,
        *,
        worker: RuntimeWorker,
        queue: RuntimeQueue,
        health: ServiceHealth | None = None,
        group_name: str = "hooklane-workers",
    ) -> None:
        self._worker = worker
        self._queue = queue
        self.health = health or ServiceHealth()
        self._group_name = group_name

    async def startup(self) -> bool:
        try:
            await self._queue.ensure_consumer_group(self._group_name)
        except EventStoreUnavailable:
            self.health.mark_started(dependency_ready=False)
            return False
        self.health.mark_started(dependency_ready=True)
        return True

    async def run_once(self) -> WorkerResult | None:
        if not self.health.begin_work():
            return None
        try:
            return await self._worker.run_once()
        finally:
            self.health.finish_work()

    def begin_shutdown(self) -> None:
        self.health.begin_shutdown()

    async def wait_for_drain(self, timeout_seconds: float) -> bool:
        return await self.health.wait_for_drain(timeout_seconds)
