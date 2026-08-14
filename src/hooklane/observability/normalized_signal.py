"""Deterministic Hooklane worker availability normalization."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from typing import Annotated, Literal, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$"
)
_FORBIDDEN_MARKERS = (
    "promql",
    "payload",
    "idempotency",
    "credential",
    "password",
    "redis://",
    "cookie",
    "secret",
    "stack",
)

SafeReference = Annotated[str, Field(pattern=_REFERENCE.pattern)]


def _safe_text(value: str, pattern: re.Pattern[str], field_name: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded safe reference")
    lowered = value.lower()
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        raise ValueError(f"{field_name} contains a forbidden operational marker")
    return value


class WorkerUnavailableObservation(BaseModel):
    """Structured evidence collected after the worker unavailable alert is observed."""

    model_config = ConfigDict(extra="forbid", strict=True)

    signal_id: str = Field(pattern=_IDENTIFIER.pattern)
    observed_at: str = Field(pattern=_TIMESTAMP.pattern)
    correlation_id: str = Field(pattern=_IDENTIFIER.pattern)
    alert_state: Literal["pending", "firing"]
    target_present: bool
    readiness_present: bool
    available_instances: int = Field(ge=0)
    required_instances: int = Field(ge=1)
    required_duration_seconds: float = Field(ge=0)
    source_ref: str = Field(pattern=_REFERENCE.pattern)
    captured_at: str | None = Field(default=None, pattern=_TIMESTAMP.pattern)
    evidence_refs: list[SafeReference] = Field(min_length=1)

    @field_validator("signal_id", "correlation_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _safe_text(value, _IDENTIFIER, info.field_name)

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        return _safe_text(value, _REFERENCE, "source_ref")

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_refs must not contain duplicates")
        return [_safe_text(item, _REFERENCE, "evidence_ref") for item in value]

    @field_validator("required_duration_seconds")
    @classmethod
    def validate_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("required_duration_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_worker_stop_evidence(self) -> WorkerUnavailableObservation:
        if self.target_present or self.readiness_present:
            raise ValueError("worker-stop normalization requires missing target and readiness")
        if self.available_instances != 0 or self.required_instances != 1:
            raise ValueError(
                "worker-stop normalization requires zero available and one required instance"
            )
        return self


def build_worker_unavailable_signal(
    observation: WorkerUnavailableObservation | Mapping[str, object],
) -> dict[str, object]:
    """Build the current ops-signal-lab normalized input shape without I/O or clock access."""

    validated = WorkerUnavailableObservation.model_validate(observation)
    source: dict[str, object] = {
        "system": "hooklane",
        "scenario": "worker-stop",
        "source_ref": validated.source_ref,
        "correlation_id": validated.correlation_id,
        "synthetic": True,
    }
    if validated.captured_at is not None:
        source["captured_at"] = validated.captured_at

    return {
        "kind": "normalized_operational_signal",
        "schema_version": "1.0",
        "signal_id": validated.signal_id,
        "signal_type": "service.availability",
        "detector_id": "HooklaneWorkerUnavailable",
        "observed_at": validated.observed_at,
        "scope": {"service": "hooklane", "components": ["worker"]},
        "evaluation": {
            "state": validated.alert_state,
            "required_duration_seconds": validated.required_duration_seconds,
        },
        "observation": {
            "kind": "condition",
            "state": "unavailable",
            "expected_state": "available",
            "reachability": "unreachable",
            "readiness": "not_ready",
            "available_instances": validated.available_instances,
            "required_instances": validated.required_instances,
        },
        "source": source,
        "evidence_refs": list(validated.evidence_refs),
    }


def normalize_worker_unavailable(
    observation: WorkerUnavailableObservation | Mapping[str, object],
) -> bytes:
    """Return canonical JSON bytes for the worker-stop availability observation."""

    signal = build_worker_unavailable_signal(observation)
    return (
        json.dumps(signal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )
