"""Validate rendered Kubernetes resiliency and workload contracts."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
from typing import Never

from chart_validate_base import CHART, KUBE_VERSION, main as validate_base
from chart_validate_base import rendered_resources, run_helm


ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY_RESOURCES = {
    ("Deployment", "hooklane-prometheus"),
    ("Deployment", "hooklane-grafana"),
    ("Service", "hooklane-prometheus"),
    ("Service", "hooklane-grafana"),
    ("ConfigMap", "hooklane-prometheus-config"),
    ("ConfigMap", "hooklane-prometheus-rules"),
    ("ConfigMap", "hooklane-grafana-provisioning"),
    ("ConfigMap", "hooklane-grafana-dashboard"),
    ("Role", "hooklane-prometheus-discovery"),
    ("RoleBinding", "hooklane-prometheus-discovery"),
}
ALLOWED_API_VERSIONS = {
    "v1",
    "apps/v1",
    "policy/v1",
    "rbac.authorization.k8s.io/v1",
}


def fail(message: str) -> Never:
    raise RuntimeError(message)


def require(document: str, key: tuple[str, str], *fragments: str) -> None:
    for fragment in fragments:
        if fragment not in document:
            fail(f"{key} is missing rendered contract: {fragment}")


def validate_kubernetes_schema(rendered: str, label: str) -> None:
    validated = subprocess.run(
        [
            "kubeconform",
            "-strict",
            "-summary",
            "-kubernetes-version",
            KUBE_VERSION,
        ],
        cwd=ROOT,
        input=rendered,
        capture_output=True,
        text=True,
    )
    if validated.returncode != 0:
        fail(f"kubeconform rejected the {label} render")


def validate_redis_secret_boundary() -> None:
    valid_values = (
        "redis://redis:6379/0",
        "rediss://redis.example:6379/0",
    )
    for value in valid_values:
        run_helm(
            "template",
            "hooklane",
            str(CHART),
            "--namespace",
            "hooklane",
            "--kube-version",
            KUBE_VERSION,
            "--set-string",
            f"config.redisURL={value}",
        )

    marker = "secret-like-value"
    authority_marker = "@"
    invalid_values = (
        f"redis://:password{authority_marker}redis.example:6379/0",
        f"redis://redis.example:6379/0?token={marker}",
        "redis://redis.example:6379/0#fragment",
        "memcached://redis.example:11211/0",
    )
    for value in invalid_values:
        completed = subprocess.run(
            [
                "helm",
                "template",
                "hooklane",
                str(CHART),
                "--namespace",
                "hooklane",
                "--kube-version",
                KUBE_VERSION,
                "--set-string",
                f"config.redisURL={value}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            fail("Helm accepted an unsafe direct Redis URL")
        if marker in f"{completed.stdout}\n{completed.stderr}":
            fail("Helm reflected a credential-like Redis URL value")

    secret_render = run_helm(
        "template",
        "hooklane",
        str(CHART),
        "--namespace",
        "hooklane",
        "--kube-version",
        KUBE_VERSION,
        "--set-string",
        "config.redisURL=redis://redis:6379/0",
        "--set",
        "config.redisURLSecret.enabled=true",
        "--set",
        "config.redisURLSecret.name=hooklane-runtime",
        "--set",
        "config.redisURLSecret.key=redis-url",
    )
    for deployment in ("name: hooklane-api", "name: hooklane-worker"):
        start = secret_render.index(deployment)
        end = secret_render.find("\n---", start)
        document = secret_render[start : len(secret_render) if end == -1 else end]
        if "value: redis://" in document or "password" in document:
            fail("Secret-enabled Deployment rendered a literal Redis URL")

    empty_secret_render = run_helm(
        "template",
        "hooklane",
        str(CHART),
        "--namespace",
        "hooklane",
        "--kube-version",
        KUBE_VERSION,
        "--set-string",
        "config.redisURL=",
        "--set",
        "config.redisURLSecret.enabled=true",
        "--set",
        "config.redisURLSecret.name=hooklane-runtime",
        "--set",
        "config.redisURLSecret.key=redis-url",
    )
    if "secretKeyRef:" not in empty_secret_render:
        fail("empty Secret-mode Redis placeholder did not render a secret reference")

    for value in (
        f"redis://:password{authority_marker}redis.example:6379/0",
        "redis://redis.example:6379/0?token=secret-like-value",
    ):
        rejected = subprocess.run(
            [
                "helm",
                "template",
                "hooklane",
                str(CHART),
                "--namespace",
                "hooklane",
                "--kube-version",
                KUBE_VERSION,
                "--set-string",
                f"config.redisURL={value}",
                "--set",
                "config.redisURLSecret.enabled=true",
                "--set",
                "config.redisURLSecret.name=hooklane-runtime",
                "--set",
                "config.redisURLSecret.key=redis-url",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if rejected.returncode == 0:
            fail("Secret-enabled Helm render accepted an unsafe Redis value")
        if "secret-like-value" in f"{rejected.stdout}\n{rejected.stderr}":
            fail("Helm reflected a credential-like Redis URL value")

    missing_secret_key = subprocess.run(
        [
            "helm",
            "template",
            "hooklane",
            str(CHART),
            "--namespace",
            "hooklane",
            "--kube-version",
            KUBE_VERSION,
            "--set",
            "config.redisURLSecret.enabled=true",
            "--set",
            "config.redisURLSecret.name=",
            "--set",
            "config.redisURLSecret.key=",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if missing_secret_key.returncode == 0:
        fail("Secret-enabled Helm render accepted missing name/key")


def validate_downstream_boundary() -> None:
    valid_values = (
        "http://hooklane-mock-sink:8080/internal/deliveries",
        "https://controlled.example/hooks",
    )
    for value in valid_values:
        run_helm(
            "template",
            "hooklane",
            str(CHART),
            "--namespace",
            "hooklane",
            "--kube-version",
            KUBE_VERSION,
            "--set-string",
            f"config.downstreamURL={value}",
        )

    marker = "secret-like-value"
    invalid_values = (
        f"https://user:{marker}@controlled.example/hooks",
        "https://controlled.example/hooks" + "?token=fixture",
        "https://controlled.example/hooks#fragment",
        "https://controlled.example/internal path",
        "https://controlled.example:not-a-port/hooks",
        "ftp://controlled.example/hooks",
    )
    for value in invalid_values:
        rejected = subprocess.run(
            [
                "helm",
                "template",
                "hooklane",
                str(CHART),
                "--namespace",
                "hooklane",
                "--kube-version",
                KUBE_VERSION,
                "--set-string",
                f"config.downstreamURL={value}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if rejected.returncode == 0:
            fail("Helm accepted an unsafe downstream URL")
        if marker in f"{rejected.stdout}\n{rejected.stderr}":
            fail("Helm reflected a credential-like downstream URL value")


def validate_render_contract(rendered: str, label: str) -> dict[tuple[str, str], str]:
    validate_kubernetes_schema(rendered, label)
    resources = rendered_resources(rendered)
    api_versions = set(
        re.findall(r"^apiVersion:\s*(\S+)\s*$", rendered, flags=re.MULTILINE)
    )
    unsupported = sorted(api_versions - ALLOWED_API_VERSIONS)
    if unsupported:
        fail(f"{label} render uses unsupported or deprecated apiVersions: {unsupported}")
    if ":latest" in rendered:
        fail(f"{label} render uses latest")
    for (kind, name), document in resources.items():
        if kind == "Secret" and ("data: {}" not in document or "stringData:" in document):
            fail(f"Secret {name} contains a value")
    return resources


def main() -> int:
    validate_base()
    validate_redis_secret_boundary()
    validate_downstream_boundary()
    rendered = run_helm(
        "template",
        "hooklane",
        str(CHART),
        "--namespace",
        "hooklane",
        "--kube-version",
        KUBE_VERSION,
    )
    resources = validate_render_contract(rendered, "default")
    configmap = resources[("ConfigMap", "hooklane-config")]
    if "HOOKLANE_REDIS_URL:" in configmap:
        fail("default ConfigMap must not contain HOOKLANE_REDIS_URL")
    workload_keys = (
        ("Deployment", "hooklane-api"),
        ("Deployment", "hooklane-worker"),
        ("Deployment", "hooklane-mock-sink"),
        ("StatefulSet", "hooklane-redis"),
    )
    probe_requirements = {
        workload_keys[0]: ("startupProbe:", "readinessProbe:", "livenessProbe:"),
        workload_keys[1]: ("startupProbe:", "readinessProbe:", "livenessProbe:"),
        workload_keys[2]: ("readinessProbe:", "livenessProbe:"),
        workload_keys[3]: ("readinessProbe:", "livenessProbe:"),
    }
    for key in workload_keys:
        document = resources[key]
        require(
            document,
            key,
            "terminationGracePeriodSeconds:",
            "resources:",
            "requests:",
            "limits:",
            *probe_requirements[key],
        )

    api = resources[workload_keys[0]]
    require(
        api,
        workload_keys[0],
        "replicas: 2",
        "type: RollingUpdate",
        "maxUnavailable: 0",
        "maxSurge: 1",
    )
    for key in workload_keys[1:3]:
        require(
            resources[key],
            key,
            "type: RollingUpdate",
            "maxUnavailable: 0",
            "maxSurge: 1",
        )
    require(
        resources[workload_keys[1]],
        workload_keys[1],
        "checksum/config:",
        "tcpSocket:",
        "port: metrics",
    )
    redis = resources[workload_keys[3]]
    require(
        redis,
        workload_keys[3],
        "kind: StatefulSet",
        "volumeClaimTemplates:",
        '"--appendonly", "yes"',
        '"--appendfsync", "always"',
    )

    expected_pdbs = {
        ("PodDisruptionBudget", "hooklane-api"): "minAvailable: 1",
        ("PodDisruptionBudget", "hooklane-worker"): "maxUnavailable: 1",
        ("PodDisruptionBudget", "hooklane-mock-sink"): "maxUnavailable: 1",
        ("PodDisruptionBudget", "hooklane-redis"): "maxUnavailable: 1",
    }
    for key, fragment in expected_pdbs.items():
        if key not in resources or fragment not in resources[key]:
            fail(f"{key} does not match the PDB contract")

    if OBSERVABILITY_RESOURCES & set(resources):
        fail("observability resources rendered while disabled")

    secret_render = run_helm(
        "template",
        "hooklane",
        str(CHART),
        "--namespace",
        "hooklane",
        "--kube-version",
        KUBE_VERSION,
        "--set",
        "config.redisURLSecret.enabled=true",
        "--set",
        "config.redisURLSecret.name=hooklane-runtime",
        "--set",
        "config.redisURLSecret.key=redis-url",
    )
    secret_resources = validate_render_contract(secret_render, "secret-injection")
    for key in (("Deployment", "hooklane-api"), ("Deployment", "hooklane-worker")):
        require(
            secret_resources[key],
            key,
            "name: HOOKLANE_REDIS_URL",
            "secretKeyRef:",
            "name: hooklane-runtime",
            "key: redis-url",
        )
    if "HOOKLANE_REDIS_URL:" in secret_resources[("ConfigMap", "hooklane-config")]:
        fail("Secret injection render must not put HOOKLANE_REDIS_URL in ConfigMap")

    enabled_render = run_helm(
        "template",
        "hooklane",
        str(CHART),
        "--namespace",
        "hooklane",
        "--kube-version",
        KUBE_VERSION,
        "--set",
        "observability.enabled=true",
    )
    enabled_resources = validate_render_contract(enabled_render, "observability-enabled")
    missing_observability = sorted(OBSERVABILITY_RESOURCES - set(enabled_resources))
    if missing_observability:
        fail(f"observability render is missing resources: {missing_observability}")

    invalid = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "--strict",
            "--set",
            "api.replicaCount=1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if invalid.returncode == 0:
        fail("values schema accepted an API replica count below two")
    invalid_observability = subprocess.run(
        [
            "helm",
            "lint",
            str(CHART),
            "--strict",
            "--set-string",
            "observability.enabled=not-a-boolean",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if invalid_observability.returncode == 0:
        fail("values schema accepted a non-boolean observability flag")
    print(
        "[ok] default/enabled render, Kubernetes schema, resources, probes, strategy, "
        "PDB, persistence, duplicate/deprecated API, and values negative contracts passed"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[fail] chart validation: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
