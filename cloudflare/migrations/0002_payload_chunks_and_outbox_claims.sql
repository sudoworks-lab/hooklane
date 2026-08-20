CREATE TABLE event_payload_chunks (
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    payload_chunk TEXT NOT NULL,
    PRIMARY KEY (event_id, chunk_index)
);

ALTER TABLE outbox ADD COLUMN claim_token TEXT;
ALTER TABLE outbox ADD COLUMN lease_until_ms INTEGER;

CREATE INDEX outbox_claim_idx
ON outbox(state, lease_until_ms, updated_at_ms, event_id);
