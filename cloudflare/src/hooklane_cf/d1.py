"""D1 implementation of the Cloudflare spike event store."""

from __future__ import annotations

import json
from time import time
from typing import Any
from uuid import UUID, uuid4

from hooklane_cf.contract import EventRequest, EventStatus, payload_json_chunks
from hooklane_cf.core import (
    AcceptanceRecord,
    DeliveryErrorClass,
    DeliveryStart,
    DeliveryStartDisposition,
    EventNotFound,
    EventRecord,
    IdempotencyConflict,
    OutboxRecord,
    PersistenceUnavailable,
)


def _row_value(row: Any, name: str) -> Any:
    try:
        return getattr(row, name)
    except (AttributeError, TypeError):
        return row[name]


class D1EventStore:
    """Persist acceptance and delivery state through the D1 binding API."""

    def __init__(
        self,
        database: Any,
        *,
        fail_dispatch_transition: bool = False,
        fail_payload_chunk: bool = False,
        fail_delivery_transition: bool = False,
    ) -> None:
        self._database = database
        self._fail_dispatch_transition = fail_dispatch_transition
        self._fail_payload_chunk = fail_payload_chunk
        self._fail_delivery_transition = fail_delivery_transition

    @staticmethod
    def _now_ms() -> int:
        return int(time() * 1000)

    async def accept(
        self,
        proposed_event_id: UUID,
        event: EventRequest,
        key_hash: str | None,
        fingerprint: str,
    ) -> AcceptanceRecord:
        now_ms = self._now_ms()
        proposed = str(proposed_event_id)
        payload_chunks = payload_json_chunks(event)
        insert_event = self._database.prepare(
            """
            INSERT INTO events (
                event_id, event_type, payload_json, status, attempt_count,
                request_fingerprint, idempotency_key_hash, accepted_at_ms, updated_at_ms
            ) VALUES (?1, ?2, ?3, 'queued', 0, ?4, ?5, ?6, ?6)
            ON CONFLICT(idempotency_key_hash) DO NOTHING
            """
        ).bind(
            proposed,
            event.event_type,
            "",
            fingerprint,
            key_hash,
            now_ms,
        )
        insert_payload_chunks = [
            self._database.prepare(
                """
                INSERT INTO event_payload_chunks (event_id, chunk_index, payload_chunk)
                SELECT ?1, ?2, ?3
                WHERE EXISTS (SELECT 1 FROM events WHERE event_id = ?1)
                ON CONFLICT(event_id, chunk_index) DO NOTHING
                """
            ).bind(
                proposed,
                -1 if self._fail_payload_chunk and index == 0 else index,
                chunk,
            )
            for index, chunk in enumerate(payload_chunks)
        ]
        insert_outbox = self._database.prepare(
            """
            INSERT INTO outbox (event_id, state, send_attempt_count, updated_at_ms)
            SELECT ?1, 'pending', 0, ?2
            WHERE EXISTS (SELECT 1 FROM events WHERE event_id = ?1)
            ON CONFLICT(event_id) DO NOTHING
            """
        ).bind(proposed, now_ms)

        if key_hash is None:
            lookup = self._database.prepare(
                """
                SELECT event_id, request_fingerprint, status, attempt_count
                FROM events WHERE event_id = ?1 LIMIT 1
                """
            ).bind(proposed)
        else:
            lookup = self._database.prepare(
                """
                SELECT event_id, request_fingerprint, status, attempt_count
                FROM events WHERE idempotency_key_hash = ?1 LIMIT 1
                """
            ).bind(key_hash)

        try:
            batch_results = await self._database.batch(
                [insert_event, *insert_payload_chunks, insert_outbox, lookup]
            )
            selected = batch_results[-1].results
            if len(selected) != 1:
                raise PersistenceUnavailable
            row = selected[0]
            stored_fingerprint = str(_row_value(row, "request_fingerprint"))
            stored_event_id = UUID(str(_row_value(row, "event_id")))
            stored_status = EventStatus(str(_row_value(row, "status")))
            attempt_count = int(_row_value(row, "attempt_count"))
        except PersistenceUnavailable:
            raise
        except Exception:
            raise PersistenceUnavailable from None

        if key_hash is not None and stored_fingerprint != fingerprint:
            raise IdempotencyConflict
        return AcceptanceRecord(
            event_id=stored_event_id,
            created=stored_event_id == proposed_event_id,
            status=stored_status,
            attempt_count=attempt_count,
        )

    async def get_status(self, event_id: UUID) -> EventRecord | None:
        try:
            row = await self._database.prepare(
                """
                SELECT event_type, payload_json, status, attempt_count
                FROM events WHERE event_id = ?1 LIMIT 1
                """
            ).bind(str(event_id)).first()
        except Exception:
            raise PersistenceUnavailable from None
        if row is None:
            return None
        try:
            payload_json = await self._load_payload_json(
                event_id,
                str(_row_value(row, "payload_json")),
            )
            event = EventRequest(
                event_type=str(_row_value(row, "event_type")),
                payload=json.loads(payload_json),
            )
            return EventRecord(
                event_id=event_id,
                event=event,
                status=EventStatus(str(_row_value(row, "status"))),
                attempt_count=int(_row_value(row, "attempt_count")),
            )
        except Exception:
            raise PersistenceUnavailable from None

    async def _load_payload_json(self, event_id: UUID, legacy_payload_json: str) -> str:
        try:
            result = await self._database.prepare(
                """
                SELECT payload_chunk FROM event_payload_chunks
                WHERE event_id = ?1 ORDER BY chunk_index
                """
            ).bind(str(event_id)).run()
            chunks = tuple(str(_row_value(row, "payload_chunk")) for row in result.results)
        except Exception:
            raise PersistenceUnavailable from None
        if chunks:
            return "".join(chunks)
        if legacy_payload_json:
            return legacy_payload_json
        raise PersistenceUnavailable

    async def claim_outbox(
        self,
        limit: int,
        claim_token: str,
        lease_ms: int,
    ) -> tuple[OutboxRecord, ...]:
        now_ms = self._now_ms()
        try:
            result = await self._database.prepare(
                """
                UPDATE outbox
                SET claim_token = ?1, lease_until_ms = ?2, updated_at_ms = ?3
                WHERE event_id IN (
                    SELECT event_id FROM outbox
                    WHERE state = 'pending'
                      AND (claim_token IS NULL OR lease_until_ms <= ?3)
                    ORDER BY updated_at_ms, event_id LIMIT ?4
                )
                RETURNING event_id, send_attempt_count
                """
            ).bind(claim_token, now_ms + lease_ms, now_ms, limit).run()
            return tuple(
                OutboxRecord(
                    event_id=UUID(str(_row_value(row, "event_id"))),
                    send_attempt_count=int(_row_value(row, "send_attempt_count")),
                    claim_token=claim_token,
                )
                for row in result.results
            )
        except Exception:
            raise PersistenceUnavailable from None

    async def mark_dispatched(self, event_id: UUID, claim_token: str | None = None) -> bool:
        if self._fail_dispatch_transition:
            self._fail_dispatch_transition = False
            raise PersistenceUnavailable
        if claim_token is None:
            claim_predicate = "claim_token IS NULL"
            bindings: tuple[object, ...] = (str(event_id), self._now_ms())
        else:
            claim_predicate = "claim_token = ?3"
            bindings = (str(event_id), self._now_ms(), claim_token)
        try:
            result = await self._database.prepare(
                f"""
                UPDATE outbox
                SET state = 'sent', send_attempt_count = send_attempt_count + 1,
                    updated_at_ms = ?2, last_reason = NULL,
                    claim_token = NULL, lease_until_ms = NULL
                WHERE event_id = ?1 AND state = 'pending' AND {claim_predicate}
                """
            ).bind(*bindings).run()
            return int(_row_value(result.meta, "changes")) == 1
        except Exception:
            raise PersistenceUnavailable from None

    async def release_outbox_claim(self, event_id: UUID, claim_token: str) -> None:
        try:
            await self._database.prepare(
                """
                UPDATE outbox
                SET claim_token = NULL, lease_until_ms = NULL,
                    updated_at_ms = ?3, last_reason = 'queue_unavailable'
                WHERE event_id = ?1 AND state = 'pending' AND claim_token = ?2
                """
            ).bind(str(event_id), claim_token, self._now_ms()).run()
        except Exception:
            raise PersistenceUnavailable from None

    async def mark_delivery_started(self, event_id: UUID, maximum_attempts: int) -> DeliveryStart:
        if maximum_attempts < 1:
            raise ValueError("maximum attempts must be positive")
        now_ms = self._now_ms()
        delivery_token = uuid4().hex
        try:
            row = await self._database.prepare(
                """
                UPDATE events
                SET status = 'delivering',
                    attempt_count = CASE
                        WHEN status = 'delivering' AND attempt_count < ?5
                            THEN attempt_count + 1
                        WHEN status = 'delivering' THEN attempt_count
                        ELSE attempt_count + 1
                    END,
                    delivery_token = ?2, delivery_lease_until_ms = ?3,
                    updated_at_ms = ?4, last_reason = NULL
                WHERE event_id = ?1
                  AND (
                    (status IN ('queued', 'retry_scheduled') AND attempt_count < ?5)
                    OR (
                        status = 'delivering'
                        AND (delivery_lease_until_ms IS NULL OR delivery_lease_until_ms <= ?4)
                        AND attempt_count <= ?5
                    )
                  )
                RETURNING event_type, payload_json, status, attempt_count
                """
            ).bind(
                str(event_id),
                delivery_token,
                now_ms + 30_000,
                now_ms,
                maximum_attempts,
            ).first()
        except Exception:
            raise PersistenceUnavailable from None
        if row is not None:
            try:
                payload_json = await self._load_payload_json(
                    event_id,
                    str(_row_value(row, "payload_json")),
                )
                return DeliveryStart(
                    disposition=DeliveryStartDisposition.STARTED,
                    status=EventStatus(str(_row_value(row, "status"))),
                    attempt_count=int(_row_value(row, "attempt_count")),
                    record=EventRecord(
                        event_id=event_id,
                        event=EventRequest(
                            event_type=str(_row_value(row, "event_type")),
                            payload=json.loads(payload_json),
                        ),
                        status=EventStatus(str(_row_value(row, "status"))),
                        attempt_count=int(_row_value(row, "attempt_count")),
                    ),
                    delivery_token=delivery_token,
                )
            except Exception:
                raise PersistenceUnavailable from None

        try:
            snapshot = await self._database.prepare(
                """
                SELECT status, attempt_count
                FROM events WHERE event_id = ?1 LIMIT 1
                """
            ).bind(str(event_id)).first()
        except Exception:
            raise PersistenceUnavailable from None
        if snapshot is None:
            raise EventNotFound
        current_status = EventStatus(str(_row_value(snapshot, "status")))
        current_attempt = int(_row_value(snapshot, "attempt_count"))
        if current_status in {EventStatus.DELIVERED, EventStatus.DEAD_LETTER}:
            return DeliveryStart(
                disposition=DeliveryStartDisposition.TERMINAL,
                status=current_status,
                attempt_count=current_attempt,
            )
        if current_status is EventStatus.DELIVERING:
            return DeliveryStart(
                disposition=DeliveryStartDisposition.BUSY,
                status=current_status,
                attempt_count=current_attempt,
            )
        if current_attempt >= maximum_attempts:
            try:
                result = await self._database.prepare(
                    """
                    UPDATE events
                    SET status = 'dead_letter', updated_at_ms = ?2,
                        last_reason = 'maximum_attempts'
                    WHERE event_id = ?1
                      AND status IN ('queued', 'retry_scheduled')
                      AND attempt_count >= ?3
                    """
                ).bind(str(event_id), now_ms, maximum_attempts).run()
            except Exception:
                raise PersistenceUnavailable from None
            if int(_row_value(result.meta, "changes")) == 1:
                return DeliveryStart(
                    disposition=DeliveryStartDisposition.TERMINAL,
                    status=EventStatus.DEAD_LETTER,
                    attempt_count=current_attempt,
                )
            # Another claimant won the CAS after the snapshot. Re-read its
            # state instead of reporting a false dead-letter disposition.
            try:
                current = await self._database.prepare(
                    """
                    SELECT status, attempt_count
                    FROM events WHERE event_id = ?1 LIMIT 1
                    """
                ).bind(str(event_id)).first()
            except Exception:
                raise PersistenceUnavailable from None
            if current is None:
                raise EventNotFound
            current_status = EventStatus(str(_row_value(current, "status")))
            current_attempt = int(_row_value(current, "attempt_count"))
            if current_status in {EventStatus.DELIVERED, EventStatus.DEAD_LETTER}:
                return DeliveryStart(
                    disposition=DeliveryStartDisposition.TERMINAL,
                    status=current_status,
                    attempt_count=current_attempt,
                )
            if current_status is EventStatus.DELIVERING:
                return DeliveryStart(
                    disposition=DeliveryStartDisposition.BUSY,
                    status=current_status,
                    attempt_count=current_attempt,
                )
        raise PersistenceUnavailable

    async def mark_delivered(self, event_id: UUID, delivery_token: str) -> bool:
        return await self._update_terminal(event_id, EventStatus.DELIVERED, None, delivery_token)

    async def mark_retry(
        self,
        event_id: UUID,
        reason: DeliveryErrorClass,
        delivery_token: str,
    ) -> bool:
        return await self._update_terminal(
            event_id,
            EventStatus.RETRY_SCHEDULED,
            reason.value,
            delivery_token,
        )

    async def mark_dead_letter(
        self,
        event_id: UUID,
        reason: DeliveryErrorClass,
        delivery_token: str,
    ) -> bool:
        return await self._update_terminal(
            event_id,
            EventStatus.DEAD_LETTER,
            reason.value,
            delivery_token,
        )

    async def _update_terminal(
        self,
        event_id: UUID,
        status: EventStatus,
        reason: str | None,
        delivery_token: str,
    ) -> bool:
        if self._fail_delivery_transition:
            self._fail_delivery_transition = False
            raise PersistenceUnavailable
        try:
            result = await self._database.prepare(
                """
                UPDATE events
                SET status = ?2, updated_at_ms = ?3, last_reason = ?4,
                    delivery_token = NULL, delivery_lease_until_ms = NULL
                WHERE event_id = ?1
                  AND status = 'delivering'
                  AND delivery_token = ?5
                """
            ).bind(str(event_id), status.value, self._now_ms(), reason, delivery_token).run()
            return int(_row_value(result.meta, "changes")) == 1
        except Exception:
            raise PersistenceUnavailable from None

    async def get_outbox_state(self, event_id: UUID) -> tuple[str, int] | None:
        try:
            row = await self._database.prepare(
                """
                SELECT state, send_attempt_count FROM outbox WHERE event_id = ?1 LIMIT 1
                """
            ).bind(str(event_id)).first()
        except Exception:
            raise PersistenceUnavailable from None
        if row is None:
            return None
        return str(_row_value(row, "state")), int(_row_value(row, "send_attempt_count"))
