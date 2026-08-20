CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'delivering', 'retry_scheduled', 'delivered', 'dead_letter')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    request_fingerprint TEXT NOT NULL,
    idempotency_key_hash TEXT UNIQUE,
    accepted_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    last_reason TEXT
);

CREATE TABLE outbox (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'sent')),
    send_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (send_attempt_count >= 0),
    updated_at_ms INTEGER NOT NULL,
    last_reason TEXT
);

CREATE INDEX events_status_idx ON events(status);
CREATE INDEX outbox_pending_idx ON outbox(state, updated_at_ms);
