from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, cast

from pydantic import BaseModel

from hooklane.domain.events import EventRequest, EventStatus


ROOT = Path(__file__).resolve().parents[2]


def _cloudflare_contract() -> ModuleType:
    path = ROOT / "cloudflare" / "src" / "hooklane_cf" / "contract.py"
    spec = importlib.util.spec_from_file_location("hooklane_cf_contract_parity", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cloudflare_event_request_schema_matches_current_public_model() -> None:
    module = _cloudflare_contract()
    cloudflare_model = cast(type[BaseModel], getattr(module, "EventRequest"))

    assert cloudflare_model.model_json_schema() == EventRequest.model_json_schema()


def test_cloudflare_status_vocabulary_matches_current_public_model() -> None:
    module = _cloudflare_contract()
    cloudflare_status = cast(Any, getattr(module, "EventStatus"))

    assert {item.value for item in cloudflare_status} == {item.value for item in EventStatus}


def test_redis_cloudflare_portfolio_comparison_is_complete() -> None:
    comparison = (ROOT / "docs" / "REDIS_CLOUDFLARE_COMPARISON.md").read_text(
        encoding="utf-8"
    )

    for dimension in (
        "| ingress |",
        "| durable state |",
        "| queue |",
        "| idempotency |",
        "| retry |",
        "| DLQ |",
        "| acceptance atomicity |",
        "| duplicate boundary |",
        "| payload handling |",
        "| failure recovery |",
        "| observability |",
        "| operational complexity |",
        "| local reproducibility |",
        "| known limits |",
    ):
        assert dimension in comparison

    for observability_concern in (
        "| request |",
        "| queue backlog |",
        "| delivery outcome |",
        "| retry |",
        "| dead-letter |",
        "| failure diagnosis |",
        "| latency |",
        "| event correlation |",
    ):
        assert observability_concern in comparison

    for contract_marker in (
        "transactional D1 payload chunks",
        "20 concurrent repair",
        "R2 payload indirection",
        "provider DLQ",
        "make cloudflare-check",
        "production readiness認定ではない",
    ):
        assert contract_marker in comparison
