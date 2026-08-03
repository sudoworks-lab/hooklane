"""Redis-backed persistence boundary for accepted events."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import time
from typing import Protocol, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from hooklane.domain.events import EventRequest, EventStatus
from hooklane.observability.logging import LogEvent, LogLevel, StructuredLogger
from hooklane.observability.metrics import HooklaneMetrics
from hooklane.runtime_config import redis_config_from_environment, parse_redis_url


class EventStoreUnavailable(Exception):
    """Raised when an accepted event cannot be persisted atomically."""


class IdempotencyConflict(Exception):
    """Raised when an idempotency key is reused for different event content."""


@dataclass(frozen=True)
class QueuedEvent:
    """Validated event claimed from the Redis Stream."""

    stream_id: str
    event_id: UUID
    event: EventRequest
    accepted_at_ms: int = 0


@dataclass(frozen=True)
class EventStatusRecord:
    """Validated public state read from Redis."""

    event_id: UUID
    status: EventStatus
    attempt_count: int


class EventStore(Protocol):
    """Persistence contract used by the event acceptance API."""

    async def enqueue(self, event_id: UUID, event: EventRequest) -> None:
        """Persist an initial status and enqueue an event."""

    async def enqueue_idempotent(
        self,
        event_id: UUID,
        event: EventRequest,
        idempotency_key: str,
    ) -> UUID:
        """Create or reuse an event through an atomic idempotency decision."""

    async def get_status(self, event_id: UUID) -> EventStatusRecord | None:
        """Return the public delivery state when the event exists."""


class RedisEventStore:
    """Store initial event state and stream data in one Redis script."""

    _ENQUEUE_SCRIPT = """
local stream_type = redis.call('TYPE', KEYS[1]).ok
local status_type = redis.call('TYPE', KEYS[2]).ok

if stream_type ~= 'none' and stream_type ~= 'stream' then
  return redis.error_reply('ERR event stream has incompatible type')
end

if status_type ~= 'none' then
  return redis.error_reply('ERR event status already exists')
end

local stream_id = redis.call(
  'XADD', KEYS[1], '*',
  'event_id', ARGV[1],
  'event_type', ARGV[2],
  'payload', ARGV[3],
  'accepted_at_ms', ARGV[5]
)
redis.call(
  'HSET', KEYS[2],
  'event_id', ARGV[1],
  'status', ARGV[4],
  'attempt_count', '0',
  'stream_id', stream_id
)
return stream_id
"""

    _IDEMPOTENT_ENQUEUE_SCRIPT = """
local stream_type = redis.call('TYPE', KEYS[1]).ok
local status_type = redis.call('TYPE', KEYS[2]).ok
local idempotency_type = redis.call('TYPE', KEYS[3]).ok

if stream_type ~= 'none' and stream_type ~= 'stream' then
  return redis.error_reply('ERR event stream has incompatible type')
end

if idempotency_type ~= 'none' and idempotency_type ~= 'hash' then
  return redis.error_reply('ERR idempotency record has incompatible type')
end

if idempotency_type == 'hash' then
  local existing_fingerprint = redis.call('HGET', KEYS[3], 'fingerprint')
  local existing_event_id = redis.call('HGET', KEYS[3], 'event_id')
  if not existing_fingerprint or not existing_event_id then
    return redis.error_reply('ERR idempotency record is incomplete')
  end
  if existing_fingerprint == ARGV[5] then
    return {'reused', existing_event_id}
  end
  return {'conflict', existing_event_id}
end

if status_type ~= 'none' then
  return redis.error_reply('ERR event status already exists')
end

local stream_id = redis.call(
  'XADD', KEYS[1], '*',
  'event_id', ARGV[1],
  'event_type', ARGV[2],
  'payload', ARGV[3],
  'accepted_at_ms', ARGV[6]
)
redis.call(
  'HSET', KEYS[2],
  'event_id', ARGV[1],
  'status', ARGV[4],
  'attempt_count', '0',
  'stream_id', stream_id
)
redis.call(
  'HSET', KEYS[3],
  'event_id', ARGV[1],
  'fingerprint', ARGV[5]
)
return {'created', ARGV[1]}
"""

    _START_DELIVERY_SCRIPT = """
if redis.call('HGET', KEYS[1], 'stream_id') ~= ARGV[1] then
  return redis.error_reply('ERR event status does not match stream message')
end
redis.call('HSET', KEYS[1], 'status', ARGV[2])
return redis.call('HINCRBY', KEYS[1], 'attempt_count', 1)
"""

    _COMPLETE_DELIVERY_SCRIPT = """
if redis.call('HGET', KEYS[1], 'stream_id') ~= ARGV[1] then
  return redis.error_reply('ERR event status does not match stream message')
end
local acknowledged = redis.call('XACK', KEYS[2], ARGV[2], ARGV[1])
if acknowledged ~= 1 then
  return redis.error_reply('ERR stream message was not pending')
end
redis.call('HSET', KEYS[1], 'status', ARGV[3])
return acknowledged
"""

    _SCHEDULE_RETRY_SCRIPT = """
local status_type = redis.call('TYPE', KEYS[1]).ok
local retry_type = redis.call('TYPE', KEYS[2]).ok
if status_type ~= 'hash' then
  return redis.error_reply('ERR event status has incompatible type')
end
if retry_type ~= 'none' and retry_type ~= 'zset' then
  return redis.error_reply('ERR retry schedule has incompatible type')
end
if redis.call('HGET', KEYS[1], 'stream_id') ~= ARGV[1] then
  return redis.error_reply('ERR event status does not match stream message')
end
redis.call(
  'HSET', KEYS[1],
  'status', ARGV[2],
  'last_error_class', ARGV[3]
)
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[5])
return 1
"""

    _RELEASE_DUE_RETRY_SCRIPT = """
local stream_type = redis.call('TYPE', KEYS[1]).ok
local retry_type = redis.call('TYPE', KEYS[2]).ok
if stream_type ~= 'stream' then
  return redis.error_reply('ERR event stream has incompatible type')
end
if retry_type == 'none' then
  return 0
end
if retry_type ~= 'zset' then
  return redis.error_reply('ERR retry schedule has incompatible type')
end
local event_ids = redis.call(
  'ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[2], 'LIMIT', 0, 1
)
if #event_ids == 0 then
  return 0
end

local event_id = event_ids[1]
local status_key = ARGV[3] .. event_id
if redis.call('HGET', status_key, 'status') ~= ARGV[5] then
  return redis.error_reply('ERR event is not retry scheduled')
end
local stream_id = redis.call('HGET', status_key, 'stream_id')
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], stream_id, stream_id, 1)
local message = redis.call('XRANGE', KEYS[1], stream_id, stream_id)
if #pending ~= 1 or #message ~= 1 then
  return redis.error_reply('ERR scheduled stream message is not pending')
end

local new_stream_id = redis.call('XADD', KEYS[1], '*', unpack(message[1][2]))
local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], stream_id)
if acknowledged ~= 1 then
  return redis.error_reply('ERR scheduled stream message was not acknowledged')
end
redis.call(
  'HSET', status_key,
  'status', ARGV[4],
  'stream_id', new_stream_id
)
redis.call('HDEL', status_key, 'last_error_class')
redis.call('ZREM', KEYS[2], event_id)
return 1
"""

    _CLAIM_STALE_PENDING_SCRIPT = """
local stream_type = redis.call('TYPE', KEYS[1]).ok
if stream_type ~= 'stream' then
  return redis.error_reply('ERR event stream has incompatible type')
end

local pending = redis.call(
  'XPENDING', KEYS[1], ARGV[1], 'IDLE', ARGV[3], '-', '+', ARGV[6]
)
for index = 1, #pending do
  local stream_id = pending[index][1]
  local message = redis.call('XRANGE', KEYS[1], stream_id, stream_id)
  if #message == 1 then
    local fields = message[1][2]
    local event_id = nil
    for field_index = 1, #fields, 2 do
      if fields[field_index] == 'event_id' then
        event_id = fields[field_index + 1]
      end
    end
    if event_id and redis.call('HGET', ARGV[4] .. event_id, 'status') == ARGV[5] then
      local claimed = redis.call(
        'XCLAIM', KEYS[1], ARGV[1], ARGV[2], ARGV[3], stream_id
      )
      if #claimed == 1 then
        return stream_id
      end
    end
  end
end
return false
"""

    _MOVE_TO_DEAD_LETTER_SCRIPT = """
local status_type = redis.call('TYPE', KEYS[1]).ok
local stream_type = redis.call('TYPE', KEYS[2]).ok
local dead_letter_type = redis.call('TYPE', KEYS[3]).ok
local retry_type = redis.call('TYPE', KEYS[4]).ok
if status_type ~= 'hash' then
  return redis.error_reply('ERR event status has incompatible type')
end
if stream_type ~= 'stream' then
  return redis.error_reply('ERR event stream has incompatible type')
end
if dead_letter_type ~= 'none' and dead_letter_type ~= 'stream' then
  return redis.error_reply('ERR dead-letter stream has incompatible type')
end
if retry_type ~= 'none' and retry_type ~= 'zset' then
  return redis.error_reply('ERR retry schedule has incompatible type')
end
if redis.call('HGET', KEYS[1], 'stream_id') ~= ARGV[1] then
  return redis.error_reply('ERR event status does not match stream message')
end
if redis.call('HGET', KEYS[1], 'status') ~= ARGV[3] then
  return redis.error_reply('ERR event is not being delivered')
end

local event_id = redis.call('HGET', KEYS[1], 'event_id')
local attempt_count = redis.call('HGET', KEYS[1], 'attempt_count')
local attempt_number = tonumber(attempt_count)
if not event_id or not attempt_number or attempt_number < 1 then
  return redis.error_reply('ERR event status is incomplete')
end
local pending = redis.call('XPENDING', KEYS[2], ARGV[2], ARGV[1], ARGV[1], 1)
local message = redis.call('XRANGE', KEYS[2], ARGV[1], ARGV[1])
if #pending ~= 1 or #message ~= 1 then
  return redis.error_reply('ERR stream message is not pending')
end

local dead_letter_fields = message[1][2]
local message_event_id = nil
for index = 1, #dead_letter_fields, 2 do
  if dead_letter_fields[index] == 'event_id' then
    message_event_id = dead_letter_fields[index + 1]
  end
end
if message_event_id ~= event_id then
  return redis.error_reply('ERR stream message has a different event ID')
end
table.insert(dead_letter_fields, 'error_class')
table.insert(dead_letter_fields, ARGV[4])
table.insert(dead_letter_fields, 'attempt_count')
table.insert(dead_letter_fields, attempt_count)
table.insert(dead_letter_fields, 'source_stream_id')
table.insert(dead_letter_fields, ARGV[1])

local dead_letter_id = redis.call(
  'XADD', KEYS[3], '*', unpack(dead_letter_fields)
)
local acknowledged = redis.call('XACK', KEYS[2], ARGV[2], ARGV[1])
if acknowledged ~= 1 then
  return redis.error_reply('ERR dead-lettered stream message was not acknowledged')
end
redis.call(
  'HSET', KEYS[1],
  'status', ARGV[5],
  'last_error_class', ARGV[4],
  'dead_letter_stream_id', dead_letter_id
)
redis.call('ZREM', KEYS[4], event_id)
return dead_letter_id
"""

    def __init__(
        self,
        client: Redis,
        *,
        namespace: str = "hooklane",
        metrics: HooklaneMetrics | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self._client = client
        self._namespace = namespace
        self._metrics = metrics
        self._logger = logger

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        namespace: str = "hooklane",
        metrics: HooklaneMetrics | None = None,
        logger: StructuredLogger | None = None,
    ) -> RedisEventStore:
        """Create a store without opening the Redis connection eagerly."""

        redis_config = parse_redis_url(url)
        return cls(
            Redis.from_url(redis_config.value, decode_responses=True),
            namespace=namespace,
            metrics=metrics,
            logger=logger,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        namespace: str = "hooklane",
        metrics: HooklaneMetrics | None = None,
        logger: StructuredLogger | None = None,
    ) -> RedisEventStore:
        """Create a store from the Secret-bound environment contract."""

        return cls.from_url(
            redis_config_from_environment().value,
            namespace=namespace,
            metrics=metrics,
            logger=logger,
        )

    def _record_redis_failure(
        self,
        operation: str,
        *,
        event_id: UUID | None = None,
    ) -> None:
        if self._metrics is not None:
            self._metrics.record_redis_failure(operation)
        if self._logger is not None:
            self._logger.emit(
                LogEvent.REDIS_OPERATION_FAILED,
                level=LogLevel.ERROR,
                event_id=event_id,
                outcome="failure",
                reason_code="redis_error",
            )

    @property
    def stream_key(self) -> str:
        """Return the Redis Stream key used for accepted events."""

        return f"{self._namespace}:events"

    def status_key(self, event_id: UUID) -> str:
        """Return the status hash key for an event."""

        return f"{self._namespace}:event:{event_id}"

    @property
    def retry_schedule_key(self) -> str:
        """Return the sorted set used for persistent retry due times."""

        return f"{self._namespace}:retries"

    @property
    def dead_letter_key(self) -> str:
        """Return the stream used for terminal delivery failures."""

        return f"{self._namespace}:dead-letter"

    def idempotency_key(self, value: str) -> str:
        """Return a Redis key that does not disclose the caller-provided key."""

        key_digest = sha256(value.encode("utf-8")).hexdigest()
        return f"{self._namespace}:idempotency:{key_digest}"

    @staticmethod
    def _request_fingerprint(event: EventRequest) -> str:
        canonical_request = json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(canonical_request.encode("utf-8")).hexdigest()

    async def enqueue(self, event_id: UUID, event: EventRequest) -> None:
        """Write stream and status records atomically without logging event data."""

        payload = json.dumps(
            event.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            await self._client.eval(
                self._ENQUEUE_SCRIPT,
                2,
                self.stream_key,
                self.status_key(event_id),
                str(event_id),
                event.event_type,
                payload,
                EventStatus.QUEUED.value,
                int(time.time() * 1000),
            )
        except RedisError:
            self._record_redis_failure("enqueue", event_id=event_id)
            raise EventStoreUnavailable from None

    async def enqueue_idempotent(
        self,
        event_id: UUID,
        event: EventRequest,
        idempotency_key: str,
    ) -> UUID:
        """Atomically create, reuse, or reject an idempotent event request."""

        payload = json.dumps(
            event.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            result = cast(
                list[str],
                await self._client.eval(
                    self._IDEMPOTENT_ENQUEUE_SCRIPT,
                    3,
                    self.stream_key,
                    self.status_key(event_id),
                    self.idempotency_key(idempotency_key),
                    str(event_id),
                    event.event_type,
                    payload,
                    EventStatus.QUEUED.value,
                    self._request_fingerprint(event),
                    int(time.time() * 1000),
                ),
            )
        except RedisError:
            self._record_redis_failure("enqueue_idempotent", event_id=event_id)
            raise EventStoreUnavailable from None

        decision, stored_event_id = result
        if decision == "conflict":
            raise IdempotencyConflict
        if decision not in {"created", "reused"}:
            raise EventStoreUnavailable
        try:
            return UUID(stored_event_id)
        except ValueError:
            self._record_redis_failure("enqueue_idempotent", event_id=event_id)
            raise EventStoreUnavailable from None

    async def get_status(self, event_id: UUID) -> EventStatusRecord | None:
        """Read and validate public state without exposing Redis internals."""

        try:
            raw_record = cast(
                dict[str, str],
                await self._client.hgetall(self.status_key(event_id)),
            )
        except RedisError:
            self._record_redis_failure("get_status", event_id=event_id)
            raise EventStoreUnavailable from None
        if not raw_record:
            return None

        try:
            stored_event_id = UUID(raw_record["event_id"])
            status = EventStatus(raw_record["status"])
            attempt_count = int(raw_record["attempt_count"])
        except (KeyError, ValueError):
            self._record_redis_failure("get_status", event_id=event_id)
            raise EventStoreUnavailable from None
        if stored_event_id != event_id or attempt_count < 0:
            self._record_redis_failure("get_status", event_id=event_id)
            raise EventStoreUnavailable
        return EventStatusRecord(
            event_id=stored_event_id,
            status=status,
            attempt_count=attempt_count,
        )

    async def ping(self) -> bool:
        """Return Redis readiness without exposing connection details."""

        try:
            return bool(await self._client.ping())
        except RedisError:
            self._record_redis_failure("ping")
            return False

    async def ensure_consumer_group(self, group_name: str) -> None:
        """Create the worker group at the beginning of the stream if needed."""

        try:
            await self._client.xgroup_create(
                self.stream_key,
                group_name,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                self._record_redis_failure("ensure_consumer_group")
                raise EventStoreUnavailable from None
        except RedisError:
            self._record_redis_failure("ensure_consumer_group")
            raise EventStoreUnavailable from None

    async def read_next(self, group_name: str, consumer_name: str) -> QueuedEvent | None:
        """Claim one new message for a consumer, leaving it pending until acked."""

        try:
            records = cast(
                list[tuple[str, list[tuple[str, dict[str, str]]]]],
                await self._client.xreadgroup(
                    group_name,
                    consumer_name,
                    {self.stream_key: ">"},
                    count=1,
                ),
            )
        except RedisError:
            self._record_redis_failure("read_next")
            raise EventStoreUnavailable from None
        if not records:
            return None

        _stream_name, messages = records[0]
        stream_id, fields = messages[0]
        try:
            payload = json.loads(fields["payload"])
            event = EventRequest(event_type=fields["event_type"], payload=payload)
            return QueuedEvent(
                stream_id=stream_id,
                event_id=UUID(fields["event_id"]),
                event=event,
                accepted_at_ms=int(fields["accepted_at_ms"]),
            )
        except (KeyError, TypeError, ValueError):
            self._record_redis_failure("read_next")
            raise EventStoreUnavailable from None

    async def claim_stale_pending(
        self,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
    ) -> QueuedEvent | None:
        """Claim one stale in-flight delivery without taking scheduled retries."""

        if min_idle_ms < 0:
            raise ValueError("pending idle threshold must not be negative")
        try:
            stream_id = cast(
                str | None,
                await self._client.eval(
                    self._CLAIM_STALE_PENDING_SCRIPT,
                    1,
                    self.stream_key,
                    group_name,
                    consumer_name,
                    min_idle_ms,
                    f"{self._namespace}:event:",
                    EventStatus.DELIVERING.value,
                    100,
                ),
            )
            if stream_id is None:
                return None
            messages = cast(
                list[tuple[str, dict[str, str]]],
                await self._client.xrange(
                    self.stream_key,
                    min=stream_id,
                    max=stream_id,
                    count=1,
                ),
            )
        except RedisError:
            self._record_redis_failure("claim_pending")
            raise EventStoreUnavailable from None
        if len(messages) != 1:
            self._record_redis_failure("claim_pending")
            raise EventStoreUnavailable

        claimed_stream_id, fields = messages[0]
        try:
            event = EventRequest(
                event_type=fields["event_type"],
                payload=json.loads(fields["payload"]),
            )
            return QueuedEvent(
                stream_id=claimed_stream_id,
                event_id=UUID(fields["event_id"]),
                event=event,
                accepted_at_ms=int(fields["accepted_at_ms"]),
            )
        except (KeyError, TypeError, ValueError):
            self._record_redis_failure("claim_pending")
            raise EventStoreUnavailable from None

    async def mark_delivery_started(self, queued_event: QueuedEvent) -> int:
        """Increment attempts and expose that the message is being delivered."""

        try:
            return int(
                await self._client.eval(
                    self._START_DELIVERY_SCRIPT,
                    1,
                    self.status_key(queued_event.event_id),
                    queued_event.stream_id,
                    EventStatus.DELIVERING.value,
                )
            )
        except RedisError:
            self._record_redis_failure(
                "mark_delivery_started",
                event_id=queued_event.event_id,
            )
            raise EventStoreUnavailable from None

    async def mark_delivered(self, queued_event: QueuedEvent, group_name: str) -> None:
        """Atomically acknowledge a successful message and update its status."""

        try:
            await self._client.eval(
                self._COMPLETE_DELIVERY_SCRIPT,
                2,
                self.status_key(queued_event.event_id),
                self.stream_key,
                queued_event.stream_id,
                group_name,
                EventStatus.DELIVERED.value,
            )
        except RedisError:
            self._record_redis_failure("mark_delivered", event_id=queued_event.event_id)
            raise EventStoreUnavailable from None

    async def schedule_retry(
        self,
        queued_event: QueuedEvent,
        due_at_ms: int,
        error_class: str,
    ) -> None:
        """Persist a retry due time while the failed message remains pending."""

        try:
            await self._client.eval(
                self._SCHEDULE_RETRY_SCRIPT,
                2,
                self.status_key(queued_event.event_id),
                self.retry_schedule_key,
                queued_event.stream_id,
                EventStatus.RETRY_SCHEDULED.value,
                error_class,
                due_at_ms,
                str(queued_event.event_id),
            )
        except RedisError:
            self._record_redis_failure("schedule_retry", event_id=queued_event.event_id)
            raise EventStoreUnavailable from None

    async def release_due_retry(self, group_name: str, now_ms: int) -> bool:
        """Atomically ack and requeue one due pending message for redelivery."""

        try:
            released = await self._client.eval(
                self._RELEASE_DUE_RETRY_SCRIPT,
                2,
                self.stream_key,
                self.retry_schedule_key,
                group_name,
                now_ms,
                f"{self._namespace}:event:",
                EventStatus.QUEUED.value,
                EventStatus.RETRY_SCHEDULED.value,
            )
        except RedisError:
            self._record_redis_failure("release_retry")
            raise EventStoreUnavailable from None
        return bool(released)

    async def move_to_dead_letter(
        self,
        queued_event: QueuedEvent,
        group_name: str,
        error_class: str,
    ) -> None:
        """Atomically preserve a terminal failure and acknowledge its message."""

        try:
            await self._client.eval(
                self._MOVE_TO_DEAD_LETTER_SCRIPT,
                4,
                self.status_key(queued_event.event_id),
                self.stream_key,
                self.dead_letter_key,
                self.retry_schedule_key,
                queued_event.stream_id,
                group_name,
                EventStatus.DELIVERING.value,
                error_class,
                EventStatus.DEAD_LETTER.value,
            )
        except RedisError:
            self._record_redis_failure("dead_letter", event_id=queued_event.event_id)
            raise EventStoreUnavailable from None

    @staticmethod
    def _stream_id_key(stream_id: str) -> tuple[int, int]:
        milliseconds, sequence = stream_id.split("-", maxsplit=1)
        return int(milliseconds), int(sequence)

    async def refresh_queue_metrics(
        self,
        group_name: str = "hooklane-workers",
    ) -> bool:
        """Refresh bounded queue gauges from Redis without failing request handling."""

        if self._metrics is None:
            return False
        try:
            if not await self._client.exists(self.stream_key):
                self._metrics.set_queue_state(depth=0, oldest_age_seconds=0, pending=0)
                return True

            groups = cast(list[dict[str, object]], await self._client.xinfo_groups(self.stream_key))
            group = next(
                (item for item in groups if str(item.get("name")) == group_name),
                None,
            )
            oldest_candidates: list[str] = []
            if group is None:
                depth = int(await self._client.xlen(self.stream_key))
                pending = 0
                if depth:
                    records = cast(
                        list[tuple[str, dict[str, str]]],
                        await self._client.xrange(self.stream_key, min="-", max="+", count=1),
                    )
                    if records:
                        oldest_candidates.append(records[0][0])
            else:
                pending = int(cast(int | str, group.get("pending", 0)))
                lag = int(cast(int | str, group.get("lag") or 0))
                depth = pending + lag
                if pending:
                    pending_entries = cast(
                        list[dict[str, object]],
                        await self._client.xpending_range(
                            self.stream_key,
                            group_name,
                            min="-",
                            max="+",
                            count=1,
                        ),
                    )
                    if pending_entries:
                        message_id = pending_entries[0].get("message_id")
                        if isinstance(message_id, str):
                            oldest_candidates.append(message_id)
                if lag:
                    last_delivered_id = str(group["last-delivered-id"])
                    unread = cast(
                        list[tuple[str, dict[str, str]]],
                        await self._client.xrange(
                            self.stream_key,
                            min=f"({last_delivered_id}",
                            max="+",
                            count=1,
                        ),
                    )
                    if unread:
                        oldest_candidates.append(unread[0][0])

            oldest_age_seconds = 0.0
            if oldest_candidates:
                oldest_id = min(oldest_candidates, key=self._stream_id_key)
                oldest_ms, _sequence = self._stream_id_key(oldest_id)
                oldest_age_seconds = max(0.0, time.time() - (oldest_ms / 1000))
            self._metrics.set_queue_state(
                depth=depth,
                oldest_age_seconds=oldest_age_seconds,
                pending=pending,
            )
            return True
        except (RedisError, KeyError, TypeError, ValueError):
            self._record_redis_failure("queue_snapshot")
            return False

    async def close(self) -> None:
        """Close Redis connections owned by this store."""

        await self._client.aclose()
