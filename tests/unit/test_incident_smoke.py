from __future__ import annotations

from incident_smoke import DRILLS, validate_documents


def test_incident_aggregate_documents_and_drill_order() -> None:
    validate_documents()

    assert [(feature_id, target) for feature_id, target, _drill in DRILLS] == [
        ("F024", "incident-downstream-5xx"),
        ("F025", "incident-redis-outage"),
        ("F026", "incident-worker-stop"),
    ]
