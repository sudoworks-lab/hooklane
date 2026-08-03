"""Prometheus application metrics with bounded label cardinality."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
)

from hooklane.observability.logging import REASON_CODES, SERVICE_NAMES


METRIC_PREFIX = "hooklane"
HTTP_METHODS = frozenset({"GET", "POST", "OTHER"})
ROUTE_TEMPLATES = frozenset(
    {
        "/v1/events",
        "/v1/events/{event_id}",
        "/health/live",
        "/health/ready",
        "/metrics",
        "/internal/deliveries",
        "unmatched",
    }
)
STATUS_CLASSES = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx"})
REDIS_OPERATIONS = frozenset(
    {
        "enqueue",
        "enqueue_idempotent",
        "get_status",
        "ping",
        "ensure_consumer_group",
        "read_next",
        "claim_pending",
        "mark_delivery_started",
        "mark_delivered",
        "schedule_retry",
        "release_retry",
        "dead_letter",
        "quarantine_message",
        "queue_snapshot",
    }
)
DELIVERY_OUTCOMES = frozenset(
    {"success", "failure", "retry_scheduled", "dead_lettered", "pending"}
)
COMPLETION_WINDOWS = frozenset({"within_60_seconds", "over_60_seconds"})

METRIC_LABELS: dict[str, tuple[str, ...]] = {
    "hooklane_http_requests_total": ("service", "method", "route", "status_class"),
    "hooklane_http_request_duration_seconds": (
        "service",
        "method",
        "route",
        "status_class",
    ),
    "hooklane_enqueue_total": ("service", "outcome", "reason_code"),
    "hooklane_queue_depth": ("service",),
    "hooklane_oldest_queued_event_age_seconds": ("service",),
    "hooklane_delivery_attempts_total": ("service",),
    "hooklane_delivery_outcomes_total": ("service", "outcome", "reason_code"),
    "hooklane_delivery_duration_seconds": ("service", "outcome"),
    "hooklane_delivery_end_to_end_duration_seconds": ("service",),
    "hooklane_delivery_completion_total": ("service", "outcome"),
    "hooklane_retry_scheduled_total": ("service", "reason_code"),
    "hooklane_dead_letter_total": ("service", "reason_code"),
    "hooklane_queue_quarantined_total": ("service", "reason_code"),
    "hooklane_worker_in_flight": ("service",),
    "hooklane_pending_messages": ("service",),
    "hooklane_redis_operation_failures_total": ("service", "operation"),
    "hooklane_service_ready": ("service",),
}


def normalize_method(method: str) -> str:
    normalized = method.upper()
    return normalized if normalized in HTTP_METHODS else "OTHER"


def normalize_route(route: str | None) -> str:
    return route if route in ROUTE_TEMPLATES else "unmatched"


def status_class(status_code: int) -> str:
    value = f"{status_code // 100}xx"
    if value not in STATUS_CLASSES:
        raise ValueError("HTTP status must be between 100 and 599")
    return value


def _bounded(value: str, allowed: frozenset[str], label_name: str) -> str:
    if value not in allowed:
        raise ValueError(f"unknown {label_name}")
    return value


class HooklaneMetrics:
    """Own an isolated registry and all Hooklane metric instruments for one service."""

    def __init__(
        self,
        service: str,
        *,
        registry: CollectorRegistry | None = None,
        include_process_metrics: bool = True,
    ) -> None:
        self.service = _bounded(service, SERVICE_NAMES, "service")
        self.registry = registry or CollectorRegistry(auto_describe=True)
        if include_process_metrics:
            ProcessCollector(namespace=METRIC_PREFIX, registry=self.registry)

        self.http_requests = Counter(
            "hooklane_http_requests_total",
            "HTTP requests completed by Hooklane.",
            METRIC_LABELS["hooklane_http_requests_total"],
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "hooklane_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            METRIC_LABELS["hooklane_http_request_duration_seconds"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
            registry=self.registry,
        )
        self.enqueue = Counter(
            "hooklane_enqueue_total",
            "Event enqueue outcomes.",
            METRIC_LABELS["hooklane_enqueue_total"],
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "hooklane_queue_depth",
            "Unacknowledged and unread event count.",
            METRIC_LABELS["hooklane_queue_depth"],
            registry=self.registry,
        )
        self.oldest_queued_age = Gauge(
            "hooklane_oldest_queued_event_age_seconds",
            "Age of the oldest unacknowledged or unread event.",
            METRIC_LABELS["hooklane_oldest_queued_event_age_seconds"],
            registry=self.registry,
        )
        self.delivery_attempts = Counter(
            "hooklane_delivery_attempts_total",
            "Delivery attempts started.",
            METRIC_LABELS["hooklane_delivery_attempts_total"],
            registry=self.registry,
        )
        self.delivery_outcomes = Counter(
            "hooklane_delivery_outcomes_total",
            "Classified delivery attempt outcomes.",
            METRIC_LABELS["hooklane_delivery_outcomes_total"],
            registry=self.registry,
        )
        self.delivery_duration = Histogram(
            "hooklane_delivery_duration_seconds",
            "Mock sink delivery attempt duration in seconds.",
            METRIC_LABELS["hooklane_delivery_duration_seconds"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.delivery_end_to_end_duration = Histogram(
            "hooklane_delivery_end_to_end_duration_seconds",
            "Accepted-to-delivered duration in seconds.",
            METRIC_LABELS["hooklane_delivery_end_to_end_duration_seconds"],
            buckets=(1, 5, 10, 30, 60, 120, 300),
            registry=self.registry,
        )
        self.delivery_completion = Counter(
            "hooklane_delivery_completion_total",
            "Delivered events classified by the 60-second SLI boundary.",
            METRIC_LABELS["hooklane_delivery_completion_total"],
            registry=self.registry,
        )
        self.retry_scheduled = Counter(
            "hooklane_retry_scheduled_total",
            "Retries scheduled after retryable delivery failures.",
            METRIC_LABELS["hooklane_retry_scheduled_total"],
            registry=self.registry,
        )
        self.dead_letter = Counter(
            "hooklane_dead_letter_total",
            "Events moved to dead letter.",
            METRIC_LABELS["hooklane_dead_letter_total"],
            registry=self.registry,
        )
        self.queue_quarantined = Counter(
            "hooklane_queue_quarantined_total",
            "Queue records removed from normal processing after bounded validation.",
            METRIC_LABELS["hooklane_queue_quarantined_total"],
            registry=self.registry,
        )
        self.worker_in_flight = Gauge(
            "hooklane_worker_in_flight",
            "Delivery attempts currently in flight.",
            METRIC_LABELS["hooklane_worker_in_flight"],
            registry=self.registry,
        )
        self.pending_messages = Gauge(
            "hooklane_pending_messages",
            "Messages pending in the Redis consumer group.",
            METRIC_LABELS["hooklane_pending_messages"],
            registry=self.registry,
        )
        self.redis_failures = Counter(
            "hooklane_redis_operation_failures_total",
            "Classified Redis operation failures.",
            METRIC_LABELS["hooklane_redis_operation_failures_total"],
            registry=self.registry,
        )
        self.service_ready = Gauge(
            "hooklane_service_ready",
            "Whether this service instance is ready for work.",
            METRIC_LABELS["hooklane_service_ready"],
            registry=self.registry,
        )

    def record_http(
        self,
        method: str,
        route: str | None,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        labels = {
            "service": self.service,
            "method": normalize_method(method),
            "route": normalize_route(route),
            "status_class": status_class(status_code),
        }
        self.http_requests.labels(**labels).inc()
        self.http_duration.labels(**labels).observe(max(0.0, duration_seconds))

    def record_enqueue(self, outcome: str, reason_code: str = "none") -> None:
        bounded_outcome = _bounded(outcome, frozenset({"success", "failure"}), "outcome")
        bounded_reason = _bounded(reason_code, REASON_CODES, "reason code")
        self.enqueue.labels(
            service=self.service,
            outcome=bounded_outcome,
            reason_code=bounded_reason,
        ).inc()

    def set_queue_state(self, *, depth: int, oldest_age_seconds: float, pending: int) -> None:
        if depth < 0 or pending < 0 or oldest_age_seconds < 0:
            raise ValueError("queue metrics must not be negative")
        self.queue_depth.labels(service=self.service).set(depth)
        self.oldest_queued_age.labels(service=self.service).set(oldest_age_seconds)
        self.pending_messages.labels(service=self.service).set(pending)

    def start_delivery(self) -> None:
        self.delivery_attempts.labels(service=self.service).inc()
        self.worker_in_flight.labels(service=self.service).inc()

    def finish_delivery(
        self,
        *,
        outcome: str,
        reason_code: str,
        duration_seconds: float,
        end_to_end_seconds: float | None = None,
    ) -> None:
        bounded_outcome = _bounded(outcome, DELIVERY_OUTCOMES, "delivery outcome")
        bounded_reason = _bounded(reason_code, REASON_CODES, "reason code")
        self.worker_in_flight.labels(service=self.service).dec()
        self.delivery_outcomes.labels(
            service=self.service,
            outcome=bounded_outcome,
            reason_code=bounded_reason,
        ).inc()
        self.delivery_duration.labels(
            service=self.service,
            outcome=bounded_outcome,
        ).observe(max(0.0, duration_seconds))
        if end_to_end_seconds is not None:
            bounded_duration = max(0.0, end_to_end_seconds)
            self.delivery_end_to_end_duration.labels(service=self.service).observe(
                bounded_duration
            )
            completion = (
                "within_60_seconds" if bounded_duration <= 60 else "over_60_seconds"
            )
            _bounded(completion, COMPLETION_WINDOWS, "completion window")
            self.delivery_completion.labels(
                service=self.service,
                outcome=completion,
            ).inc()

    def record_retry(self, reason_code: str) -> None:
        reason = _bounded(reason_code, REASON_CODES, "reason code")
        self.retry_scheduled.labels(service=self.service, reason_code=reason).inc()

    def record_dead_letter(self, reason_code: str) -> None:
        reason = _bounded(reason_code, REASON_CODES, "reason code")
        self.dead_letter.labels(service=self.service, reason_code=reason).inc()

    def record_queue_quarantine(self, reason_code: str) -> None:
        reason = _bounded(reason_code, REASON_CODES, "reason code")
        self.queue_quarantined.labels(service=self.service, reason_code=reason).inc()

    def record_redis_failure(self, operation: str) -> None:
        bounded_operation = _bounded(operation, REDIS_OPERATIONS, "Redis operation")
        self.redis_failures.labels(
            service=self.service,
            operation=bounded_operation,
        ).inc()

    def set_ready(self, ready: bool) -> None:
        self.service_ready.labels(service=self.service).set(1 if ready else 0)

    def render(self) -> bytes:
        return generate_latest(self.registry)
