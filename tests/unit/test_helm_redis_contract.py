from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "hooklane"
KUBE_VERSION = "1.34.8"


def render(*values: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "helm",
            "template",
            "hooklane",
            str(CHART),
            "--namespace",
            "hooklane",
            "--kube-version",
            KUBE_VERSION,
            *values,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_credential_free_redis_urls_render() -> None:
    for value in ("redis://redis:6379/0", "rediss://host:6379/0"):
        result = render("--set-string", f"config.redisURL={value}")
        assert result.returncode == 0, result.stderr


def test_unsafe_direct_redis_urls_fail_without_reflection() -> None:
    marker = "secret-like-value"
    authority_marker = "@"
    for value in (
        f"redis://:password{authority_marker}host:6379/0",
        "redis://host:6379/0?decode_responses=true",
        f"redis://host:6379/0?token={marker}",
        "redis://host:6379/0#fragment",
        "memcached://host:11211/0",
    ):
        result = render("--set-string", f"config.redisURL={value}")
        assert result.returncode != 0
        assert marker not in f"{result.stdout}\n{result.stderr}"


def test_secret_injection_rejects_credential_bearing_values() -> None:
    result = render(
        "--set-string",
        "config.redisURL=redis://:password@host:6379/0",
        "--set",
        "config.redisURLSecret.enabled=true",
        "--set",
        "config.redisURLSecret.name=hooklane-runtime",
        "--set",
        "config.redisURLSecret.key=redis-url",
    )

    assert result.returncode != 0
    assert "password" not in f"{result.stdout}\n{result.stderr}"


def test_secret_injection_uses_only_secret_reference() -> None:
    result = render(
        "--set-string",
        "config.redisURL=redis://redis:6379/0",
        "--set",
        "config.redisURLSecret.enabled=true",
        "--set",
        "config.redisURLSecret.name=hooklane-runtime",
        "--set",
        "config.redisURLSecret.key=redis-url",
    )

    assert result.returncode == 0, result.stderr
    assert "secretKeyRef:" in result.stdout
    assert "value: redis://" not in result.stdout


def test_secret_injection_accepts_empty_explicit_placeholder() -> None:
    result = render(
        "--set-string",
        "config.redisURL=",
        "--set",
        "config.redisURLSecret.enabled=true",
        "--set",
        "config.redisURLSecret.name=hooklane-runtime",
        "--set",
        "config.redisURLSecret.key=redis-url",
    )

    assert result.returncode == 0, result.stderr
    assert "secretKeyRef:" in result.stdout


def test_secret_injection_requires_name_and_key() -> None:
    result = render(
        "--set-string",
        "config.redisURL=redis://redis:6379/0",
        "--set",
        "config.redisURLSecret.enabled=true",
        "--set",
        "config.redisURLSecret.name=",
        "--set",
        "config.redisURLSecret.key=redis-url",
    )

    assert result.returncode != 0
    assert "name and key" in f"{result.stdout}\n{result.stderr}"


def test_downstream_url_boundary_rejects_unsafe_configmap_values() -> None:
    marker = "secret-like-value"
    for value in (
        f"https://user:{marker}@controlled.example/hooks",
        "https://controlled.example/hooks" + "?token=fixture",
        "https://controlled.example/hooks#fragment",
        "https://controlled.example/internal path",
        "https://controlled.example:not-a-port/hooks",
        "ftp://controlled.example/hooks",
    ):
        result = render("--set-string", f"config.downstreamURL={value}")
        assert result.returncode != 0
        assert marker not in f"{result.stdout}\n{result.stderr}"


def test_downstream_url_boundary_renders_safe_configmap_value() -> None:
    result = render(
        "--set-string",
        "config.downstreamURL=http://hooklane-mock-sink:8080/internal/deliveries",
    )

    assert result.returncode == 0, result.stderr
    assert "HOOKLANE_DOWNSTREAM_URL:" in result.stdout
    assert "hooklane-mock-sink:8080" in result.stdout
