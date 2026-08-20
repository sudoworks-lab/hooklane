from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wrangler_configuration_is_local_safe_and_has_dlq() -> None:
    configuration = json.loads((ROOT / "wrangler.jsonc").read_text(encoding="utf-8"))

    assert configuration["compatibility_flags"] == ["python_workers"]
    assert configuration["main"] == "src/worker.py"
    assert configuration["workers_dev"] is False
    assert configuration["vars"]["SPIKE_TEST_MODE"] == "false"
    assert configuration["d1_databases"] == [
        {
            "binding": "DB",
            "database_name": "hooklane-spike-db",
            "database_id": "00000000-0000-0000-0000-000000000001",
            "migrations_dir": "migrations",
        }
    ]
    assert configuration["queues"]["producers"] == [
        {"binding": "DELIVERY_QUEUE", "queue": "hooklane-spike-delivery"}
    ]
    assert configuration["queues"]["consumers"][0]["max_retries"] == 4
    assert configuration["queues"]["consumers"][0]["dead_letter_queue"] == (
        "hooklane-spike-dlq"
    )
    assert "routes" not in configuration
    assert "account_id" not in configuration
    assert "r2_buckets" not in configuration
    assert "durable_objects" not in configuration
    assert all("remote" not in database for database in configuration["d1_databases"])


def test_d1_schema_contains_event_idempotency_payload_chunks_and_claims() -> None:
    initial = (ROOT / "migrations" / "0001_initial.sql").read_text(encoding="utf-8")
    extension = (
        ROOT / "migrations" / "0002_payload_chunks_and_outbox_claims.sql"
    ).read_text(encoding="utf-8")

    for marker in (
        "event_id TEXT PRIMARY KEY",
        "payload_json TEXT NOT NULL",
        "attempt_count INTEGER NOT NULL",
        "request_fingerprint TEXT NOT NULL",
        "idempotency_key_hash TEXT UNIQUE",
        "CREATE TABLE outbox",
        "state IN ('pending', 'sent')",
        "send_attempt_count INTEGER NOT NULL",
    ):
        assert marker in initial
    for marker in (
        "CREATE TABLE event_payload_chunks",
        "PRIMARY KEY (event_id, chunk_index)",
        "ALTER TABLE outbox ADD COLUMN claim_token TEXT",
        "ALTER TABLE outbox ADD COLUMN lease_until_ms INTEGER",
        "CREATE INDEX outbox_claim_idx",
    ):
        assert marker in extension
    delivery_claims = (
        ROOT / "migrations" / "0003_delivery_attempt_claims.sql"
    ).read_text(encoding="utf-8")
    for marker in (
        "ALTER TABLE events ADD COLUMN delivery_token TEXT",
        "ALTER TABLE events ADD COLUMN delivery_lease_until_ms INTEGER",
        "CREATE INDEX events_delivery_claim_idx",
    ):
        assert marker in delivery_claims
    assert "idempotency_key TEXT" not in initial + extension


def test_local_only_interfaces_require_explicit_test_mode() -> None:
    worker = (ROOT / "src" / "worker.py").read_text(encoding="utf-8")

    assert "getattr(env, \"SPIKE_TEST_MODE\", \"false\")" in worker
    assert worker.count("if not _test_mode(env):") >= 3
    assert "/__spike/delivery-regression" in worker


def test_destination_is_operator_configuration_not_request_input() -> None:
    worker = (ROOT / "src" / "worker.py").read_text(encoding="utf-8")
    contract = (ROOT / "src" / "hooklane_cf" / "contract.py").read_text(encoding="utf-8")

    assert "self.env.MOCK_SINK_URL" in worker
    assert 'redirect="manual"' in worker
    assert "destination" not in contract
