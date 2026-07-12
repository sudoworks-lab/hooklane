from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from container_policy_check import load_policy, verify_helm_exceptions  # noqa: E402


def test_unregistered_helm_limitation_fails_policy() -> None:
    policy = deepcopy(load_policy())
    exceptions = policy["exceptions"]
    assert isinstance(exceptions, list)
    policy["exceptions"] = [
        entry
        for entry in exceptions
        if not (
            isinstance(entry, dict)
            and entry.get("service") == "redis"
            and entry.get("field") == "high_availability"
        )
    ]

    with pytest.raises(RuntimeError, match="not registered"):
        verify_helm_exceptions(policy)
