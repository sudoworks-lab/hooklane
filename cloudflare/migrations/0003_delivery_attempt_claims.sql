ALTER TABLE events ADD COLUMN delivery_token TEXT;
ALTER TABLE events ADD COLUMN delivery_lease_until_ms INTEGER;

CREATE INDEX events_delivery_claim_idx
ON events(status, delivery_lease_until_ms, event_id);
