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


@pytest.mark.parametrize(
    "suffix",
    (
        "?ssl_cert_reqs=none",
        "?ssl_check_hostname=false",
        "?decode_responses=true",
        "?password=secret-like-value",
        "#fragment",
    ),
)
def test_redis_url_rejects_query_and_fragment_without_reflection(suffix: str) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        parse_redis_url(f"rediss://redis.example:6380/0{suffix}")

    assert "secret-like-value" not in str(error.value)


@pytest.mark.parametrize(
    "value",
    (
        "redis://redis.example:6380/0 with-space",
        "redis://redis.example:not-a-port/0",
        "memcached://redis.example:11211/0",
    ),
)
def test_redis_url_rejects_whitespace_invalid_port_and_unsupported_scheme(
    value: str,
) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        parse_redis_url(value)

    assert value not in str(error.value)


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


@pytest.mark.parametrize(
    "value",
    (
        "https://controlled.example/internal path",
        "https://controlled.example/\tpath",
        "https://controlled.example/hooks" + "?token=fixture",
        "https://controlled.example/hooks#fragment",
        "https://controlled.example:not-a-port/hooks",
    ),
)
def test_downstream_rejects_any_whitespace_and_unsafe_url_parts(value: str) -> None:
    with pytest.raises(RuntimeConfigurationError) as error:
        parse_downstream_url(value)

    assert value not in str(error.value)
