"""Environment-backed runtime configuration with safe validation boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit, urlunsplit


DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_DOWNSTREAM_URL = "http://hooklane-mock-sink:8080/internal/deliveries"


class RuntimeConfigurationError(ValueError):
    """Raised when an environment-backed runtime value is invalid."""


@dataclass(frozen=True)
class RedisConnectionConfig:
    """Validated Redis connection settings without exposing the URL in repr output."""

    value: str = field(repr=False)
    scheme: str
    tls_enabled: bool
    has_credentials: bool


@dataclass(frozen=True)
class DownstreamTargetConfig:
    """Validated downstream endpoint without exposing the URL in repr output."""

    value: str = field(repr=False)
    origin: str


def parse_redis_url(value: str) -> RedisConnectionConfig:
    """Validate a Redis URL without logging or reflecting its value."""

    if not value or value != value.strip():
        raise RuntimeConfigurationError("Redis URL must be a non-empty URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise RuntimeConfigurationError("Redis URL is invalid") from None
    if parsed.scheme not in {"redis", "rediss"} or hostname is None:
        raise RuntimeConfigurationError("Redis URL must use redis:// or rediss://")
    return RedisConnectionConfig(
        value=value,
        scheme=parsed.scheme,
        tls_enabled=parsed.scheme == "rediss",
        has_credentials=parsed.username is not None or parsed.password is not None,
    )


def redis_config_from_environment(
    environment: Mapping[str, str] | None = None,
) -> RedisConnectionConfig:
    """Read the Redis URL from the environment, preserving local defaults."""

    source = os.environ if environment is None else environment
    return parse_redis_url(source.get("HOOKLANE_REDIS_URL", DEFAULT_REDIS_URL))


def parse_downstream_url(value: str) -> DownstreamTargetConfig:
    """Validate a controlled downstream endpoint without accepting credentials."""

    if not value or value != value.strip():
        raise RuntimeConfigurationError("Downstream URL must be a non-empty URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise RuntimeConfigurationError("Downstream URL is invalid") from None
    if parsed.scheme not in {"http", "https"} or hostname is None:
        raise RuntimeConfigurationError("Downstream URL must use http:// or https://")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeConfigurationError("Downstream URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeConfigurationError("Downstream URL must not contain query or fragment")
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return DownstreamTargetConfig(value=value, origin=origin)


def downstream_config_from_environment(
    environment: Mapping[str, str] | None = None,
) -> DownstreamTargetConfig:
    """Read the controlled downstream endpoint, defaulting to the mock sink."""

    source = os.environ if environment is None else environment
    return parse_downstream_url(
        source.get("HOOKLANE_DOWNSTREAM_URL", DEFAULT_DOWNSTREAM_URL)
    )
