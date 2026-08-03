"""Executable worker process for local container runtimes."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import os
import socket

from prometheus_client import start_http_server

from hooklane.delivery.dead_letter import DeadLetterPolicy
from hooklane.delivery.retry import RetryPolicy
from hooklane.delivery.sink import MockSinkClient
from hooklane.observability.logging import LogEvent, StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics
from hooklane.queue.events import EventStoreUnavailable, RedisEventStore
from hooklane.runtime import ShutdownCoordinator
from hooklane.worker.runtime import WorkerRuntime
from hooklane.worker.service import EventWorker, WorkerResult


IDLE_POLL_SECONDS = 0.1
DEFAULT_METRICS_PORT = 9090


@dataclass(frozen=True)
class WorkerSettings:
    maximum_attempts: int
    pending_idle_ms: int


def load_worker_settings(environment: Mapping[str, str]) -> WorkerSettings:
    maximum_attempts = int(environment.get("HOOKLANE_RETRY_MAXIMUM_ATTEMPTS", "5"))
    pending_idle_ms = int(environment.get("HOOKLANE_PENDING_IDLE_MILLISECONDS", "60000"))
    if maximum_attempts < 1:
        raise ValueError("maximum attempts must be positive")
    if pending_idle_ms < 0:
        raise ValueError("pending idle milliseconds must not be negative")
    return WorkerSettings(
        maximum_attempts=maximum_attempts,
        pending_idle_ms=pending_idle_ms,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Hooklane delivery worker")
    return parser.parse_args()


async def wait_for_work_or_shutdown(coordinator: ShutdownCoordinator) -> None:
    try:
        await asyncio.wait_for(coordinator.wait(), timeout=IDLE_POLL_SECONDS)
    except TimeoutError:
        pass


async def run_worker() -> int:
    settings = load_worker_settings(os.environ)
    metrics = HooklaneMetrics("worker")
    logger = StructuredLogger("worker")
    store = RedisEventStore.from_environment(
        metrics=metrics,
        logger=logger,
    )
    sink = MockSinkClient()
    worker = EventWorker(
        store,
        sink,
        consumer_name=os.environ.get("HOOKLANE_CONSUMER_NAME", socket.gethostname()),
        retry_policy=RetryPolicy(),
        dead_letter_policy=DeadLetterPolicy(maximum_attempts=settings.maximum_attempts),
        pending_idle_ms=settings.pending_idle_ms,
        metrics=metrics,
        logger=logger,
    )
    runtime = WorkerRuntime(worker=worker, queue=store)
    coordinator = ShutdownCoordinator(on_shutdown=runtime.begin_shutdown)
    coordinator.install(asyncio.get_running_loop())
    metrics_port = int(os.environ.get("HOOKLANE_METRICS_PORT", str(DEFAULT_METRICS_PORT)))
    if not 1 <= metrics_port <= 65_535:
        raise ValueError("metrics port must be between 1 and 65535")
    metrics_server, metrics_thread = start_http_server(
        metrics_port,
        addr="0.0.0.0",
        registry=metrics.registry,
    )
    logger.emit(
        LogEvent.WORKER_STARTED,
        outcome="success",
        reason_code="none",
    )

    try:
        while not coordinator.requested:
            if not runtime.health.ready and not await runtime.startup():
                metrics.set_ready(False)
                await wait_for_work_or_shutdown(coordinator)
                continue
            metrics.set_ready(True)
            try:
                result = await runtime.run_once()
            except EventStoreUnavailable:
                runtime.health.set_dependency_ready(False)
                metrics.set_ready(False)
                await wait_for_work_or_shutdown(coordinator)
                continue
            if result in {None, WorkerResult.NO_MESSAGE}:
                await wait_for_work_or_shutdown(coordinator)
        await runtime.wait_for_drain(timeout_seconds=10.0)
    finally:
        metrics.set_ready(False)
        logger.emit(
            LogEvent.WORKER_STOPPED,
            outcome="success",
            reason_code="none",
        )
        metrics_server.shutdown()
        metrics_server.server_close()
        metrics_thread.join(timeout=5)
        await sink.close()
        await store.close()
    return 0


def main() -> int:
    parse_args()
    return asyncio.run(run_worker())


if __name__ == "__main__":
    raise SystemExit(main())
