from __future__ import annotations

from uuid import uuid4

import pytest

from hooklane.runtime_config import (
    DEFAULT_DOWNSTREAM_URL,
    DEFAULT_REDIS_URL,
    RuntimeConfigurationError,
    downstream_config_from_environment,
    parse_downstream_url,
    parse_redis_url,
    redis_config_from_environment,
)


def test_redis_default_is_backward_compatible() -> None:
    config = redis_config_from_environment({})

    assert config.value == DEFAULT_REDIS_URL
    assert config.scheme == "redis"
    assert config.tls_enabled is False
    assert config.has_credentials is False


def test_rediss_url_marks_tls_and_authentication_without_repr_leak() -> None:
    marker = uuid4().hex
    config = parse_redis_url(f"rediss://runtime-user:{marker}@redis.example:6380/0")

    assert config.scheme == "rediss"
    assert config.tls_enabled is True
    assert config.has_credentials is True
    assert marker not in repr(config)


def test_redis_url_rejects_unsupported_scheme_without_reflection() -> None:
    marker = uuid4().hex

    with pytest.raises(RuntimeConfigurationError) as error:
        parse_redis_url(f"https://redis.example:6379/0?password={marker}")

    assert marker not in str(error.value)


def test_downstream_default_is_the_existing_mock_sink() -> None:
    config = downstream_config_from_environment({})

    assert config.value == DEFAULT_DOWNSTREAM_URL
    assert config.origin == "http://hooklane-mock-sink:8080"
    assert DEFAULT_DOWNSTREAM_URL not in repr(config)


def test_downstream_environment_can_select_a_controlled_endpoint() -> None:
    config = downstream_config_from_environment(
        {"HOOKLANE_DOWNSTREAM_URL": "https://controlled.example/hooks"}
    )

    assert config.value == "https://controlled.example/hooks"
    assert config.origin == "https://controlled.example"


def test_downstream_credentials_are_rejected_without_reflection() -> None:
    marker = uuid4().hex

    with pytest.raises(RuntimeConfigurationError) as error:
        parse_downstream_url(f"https://user:{marker}@controlled.example/hooks")

    assert marker not in str(error.value)
