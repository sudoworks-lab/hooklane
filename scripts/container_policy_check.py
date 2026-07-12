"""Validate the machine-readable container hardening baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Never, cast


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_IMAGES = (
    "hooklane-api:0.1.0",
    "hooklane-worker:0.1.0",
    "hooklane-mock-sink:0.1.0",
)


def fail(message: str) -> Never:
    raise RuntimeError(message)


def load_policy() -> dict[str, Any]:
    data: object = json.loads(
        (ROOT / "container-policy.json").read_text(encoding="utf-8")
    )
    if not isinstance(data, dict):
        fail("policy root must be an object")
    return cast(dict[str, Any], data)


def verify_policy_document(policy: dict[str, Any]) -> None:
    expected_defaults = {
        "run_as_non_root": True,
        "allow_privilege_escalation": False,
        "drop_capabilities": ["ALL"],
        "read_only_root_filesystem": True,
        "seccomp_profile": "RuntimeDefault",
        "writable_paths": ["/tmp"],
    }
    if policy.get("version") != 1 or policy.get("defaults") != expected_defaults:
        fail("container defaults do not match the baseline")

    services_object = policy.get("services")
    if not isinstance(services_object, dict):
        fail("services must be an object")
    services = cast(dict[str, Any], services_object)
    required_services = {"api", "worker", "mock-sink", "redis", "prometheus", "grafana"}
    if set(services) != required_services:
        fail("policy must cover every current and planned service")
    service_entries: dict[str, dict[str, Any]] = {}
    for name, entry in services.items():
        if not isinstance(entry, dict):
            fail(f"{name} policy must be an object")
        service_entries[name] = cast(dict[str, Any], entry)
    for service in ("api", "worker", "mock-sink"):
        image = service_entries[service].get("image")
        if not isinstance(image, str) or ":0.1.0" not in image or image.endswith(":latest"):
            fail(f"{service} image is not fixed")
    redis_image = service_entries["redis"].get("image")
    if not isinstance(redis_image, str) or "@sha256:" not in redis_image:
        fail("Redis image must be fixed by version and digest")
    expected_observability_images = {
        "prometheus": (
            "prom/prometheus:v3.12.0-distroless@sha256:"
            "f39df5334dee301b885f77e0ff1159f5d8a43bf9db518f885544594799a1e3c2"
        ),
        "grafana": (
            "grafana/grafana:13.0.2@sha256:"
            "5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2"
        ),
    }
    for service, expected_image in expected_observability_images.items():
        entry = service_entries[service]
        if entry.get("image") != expected_image or entry.get("lifecycle") != "implemented":
            fail(f"{service} image and lifecycle must match the F017 implementation")

    exceptions_object = policy.get("exceptions")
    if not isinstance(exceptions_object, list):
        fail("exceptions must be a list")
    exception_entries: list[dict[str, Any]] = []
    for entry_object in exceptions_object:
        entry = entry_object
        if not isinstance(entry, dict):
            fail("every exception must be an object")
        typed_entry = cast(dict[str, Any], entry)
        for field in ("service", "field", "scope", "reason"):
            value = typed_entry.get(field)
            if not isinstance(value, str) or not value.strip():
                fail(f"container exception is missing {field}")
        if typed_entry["service"] not in required_services:
            fail("container exception names an unknown service")
        exception_entries.append(typed_entry)
    exception_services = {entry["service"] for entry in exception_entries}
    if not {"redis", "prometheus", "grafana"}.issubset(exception_services):
        fail("all current or planned policy exceptions must be explained")


def verify_application_images() -> None:
    for image_name in APPLICATION_IMAGES:
        completed = subprocess.run(
            ["docker", "image", "inspect", image_name],
            check=True,
            capture_output=True,
            text=True,
        )
        inspected = json.loads(completed.stdout)
        config = inspected[0]["Config"]
        if config.get("User") != "10001:10001":
            fail(f"{image_name} is not configured as non-root")
        if image_name.endswith(":latest"):
            fail(f"{image_name} is not fixed")


def compose_config() -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    data: object = json.loads(completed.stdout)
    if not isinstance(data, dict):
        fail("Compose config root must be an object")
    return cast(dict[str, Any], data)


def verify_compose() -> None:
    config = compose_config()
    services_object = config.get("services")
    if not isinstance(services_object, dict):
        fail("Compose services must be an object")
    services = cast(dict[str, Any], services_object)
    required = {"api", "worker", "mock-sink", "redis"}
    if set(services) != required:
        fail("Compose must define exactly the four local services")

    for service_name, service_object in services.items():
        if not isinstance(service_object, dict):
            fail(f"{service_name} Compose policy must be an object")
        service = cast(dict[str, Any], service_object)
        image = service.get("image")
        if not isinstance(image, str) or image.endswith(":latest"):
            fail(f"{service_name} Compose image is not fixed")
        if service.get("read_only") is not True:
            fail(f"{service_name} root filesystem is not read-only")
        if service.get("cap_drop") != ["ALL"]:
            fail(f"{service_name} must drop all capabilities")
        security_options = service.get("security_opt")
        if not isinstance(security_options, list) or "no-new-privileges:true" not in security_options:
            fail(f"{service_name} must prevent privilege escalation")
        user = service.get("user")
        if not isinstance(user, str) or user.split(":", maxsplit=1)[0] in {"", "0", "root"}:
            fail(f"{service_name} must use a non-root runtime user")
        if not isinstance(service.get("healthcheck"), dict):
            fail(f"{service_name} must define a healthcheck")
        tmpfs = service.get("tmpfs")
        if not isinstance(tmpfs, list) or not any(
            isinstance(entry, str) and entry.startswith("/tmp:") for entry in tmpfs
        ):
            fail(f"{service_name} must declare /tmp as its writable path")
        ports = service.get("ports") or []
        if not isinstance(ports, list):
            fail(f"{service_name} ports must be a list")
        for port_object in ports:
            if not isinstance(port_object, dict) or port_object.get("host_ip") != "127.0.0.1":
                fail(f"{service_name} published ports must bind to loopback")

    api_image = cast(dict[str, Any], services["api"])["image"]
    worker_image = cast(dict[str, Any], services["worker"])["image"]
    sink_image = cast(dict[str, Any], services["mock-sink"])["image"]
    redis_service = cast(dict[str, Any], services["redis"])
    if (api_image, worker_image, sink_image) != APPLICATION_IMAGES:
        fail("Compose application image tags do not match the image contract")
    redis_image = redis_service.get("image")
    if not isinstance(redis_image, str) or "redis:8.0.1-alpine@sha256:" not in redis_image:
        fail("Compose Redis image is not fixed by version and digest")
    redis_tmpfs = redis_service.get("tmpfs")
    if not isinstance(redis_tmpfs, list) or not any(
        isinstance(entry, str) and entry.startswith("/data:") for entry in redis_tmpfs
    ):
        fail("Compose Redis writable data path must be an ephemeral tmpfs")
    if config.get("volumes"):
        fail("Compose must not leave persistent local volumes")


def helm_render(*, observability: bool = False) -> str:
    command = [
        "helm",
        "template",
        "hooklane",
        str(ROOT / "charts" / "hooklane"),
        "--namespace",
        "hooklane",
        "--kube-version",
        "1.34.8",
    ]
    if observability:
        command.extend(["--set", "observability.enabled=true"])
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def helm_resources(rendered: str) -> dict[tuple[str, str], str]:
    resources: dict[tuple[str, str], str] = {}
    for document in re.split(r"^---\s*$", rendered, flags=re.MULTILINE):
        kind_match = re.search(r"^kind:\s*(\S+)\s*$", document, flags=re.MULTILINE)
        name_match = re.search(
            r"^metadata:\s*\n(?:^[ \t]+.*\n)*?^[ \t]+name:\s*(\S+)\s*$",
            document,
            flags=re.MULTILINE,
        )
        if kind_match and name_match:
            resources[(kind_match.group(1), name_match.group(1))] = document
    return resources


def verify_helm_exceptions(policy: dict[str, Any]) -> None:
    entries = policy.get("exceptions")
    if not isinstance(entries, list):
        fail("Helm policy exceptions must be a list")
    registered = {
        (entry.get("service"), entry.get("field"))
        for entry in entries
        if isinstance(entry, dict)
    }
    required = {
        ("worker", "pdb_availability"),
        ("mock-sink", "pdb_availability"),
        ("redis", "high_availability"),
    }
    missing = sorted(required - registered)
    if missing:
        fail(f"Helm availability limitations are not registered: {missing}")


def verify_helm(policy: dict[str, Any]) -> None:
    verify_helm_exceptions(policy)
    rendered = helm_render()
    resources = helm_resources(rendered)
    workloads = {
        ("Deployment", "hooklane-api"),
        ("Deployment", "hooklane-worker"),
        ("Deployment", "hooklane-mock-sink"),
        ("StatefulSet", "hooklane-redis"),
        ("Pod", "hooklane-test"),
    }
    missing = sorted(workloads - set(resources))
    if missing:
        fail(f"Helm render is missing hardened workloads: {missing}")
    forbidden = (
        "privileged: true",
        "hostNetwork: true",
        "hostPID: true",
        "hostIPC: true",
        "hostPath:",
    )
    for key in workloads:
        document = resources[key]
        required_fragments = (
            "automountServiceAccountToken: false",
            "runAsNonRoot: true",
            "runAsUser:",
            "runAsGroup:",
            "seccompProfile:",
            "type: RuntimeDefault",
            "allowPrivilegeEscalation: false",
            "readOnlyRootFilesystem: true",
            'drop: ["ALL"]',
            "resources:",
            "requests:",
            "limits:",
            "volumeMounts:",
            "emptyDir:",
        )
        for fragment in required_fragments:
            if fragment not in document:
                fail(f"{key} is missing Helm hardening field: {fragment}")
        for fragment in forbidden:
            if fragment in document:
                fail(f"{key} contains forbidden Helm field: {fragment}")
        if ":latest" in document:
            fail(f"{key} uses latest")
    service_account = resources.get(("ServiceAccount", "hooklane"), "")
    if "automountServiceAccountToken: false" not in service_account:
        fail("Helm ServiceAccount must disable token automount")
    redis = resources[("StatefulSet", "hooklane-redis")]
    if "volumeClaimTemplates:" not in redis:
        fail("Helm Redis workload must declare persistent storage")


def verify_observability(policy: dict[str, Any]) -> None:
    entries = policy.get("exceptions")
    if not isinstance(entries, list):
        fail("observability policy exceptions must be a list")
    registered = {
        (entry.get("service"), entry.get("field"))
        for entry in entries
        if isinstance(entry, dict)
    }
    required_exceptions = {
        ("prometheus", "writable_paths"),
        ("prometheus", "service_account_token_projection"),
        ("grafana", "writable_paths"),
        ("grafana", "anonymous_access"),
    }
    missing_exceptions = sorted(required_exceptions - registered)
    if missing_exceptions:
        fail(f"observability exceptions are not registered: {missing_exceptions}")

    resources = helm_resources(helm_render(observability=True))
    workloads = {
        ("Deployment", "hooklane-prometheus"),
        ("Deployment", "hooklane-grafana"),
    }
    for key in workloads:
        document = resources.get(key)
        if document is None:
            fail(f"observability render is missing {key}")
        for fragment in (
            "automountServiceAccountToken: false",
            "runAsNonRoot: true",
            "runAsUser:",
            "runAsGroup:",
            "type: RuntimeDefault",
            "allowPrivilegeEscalation: false",
            "readOnlyRootFilesystem: true",
            'drop: ["ALL"]',
            "resources:",
            "requests:",
            "limits:",
            "mountPath: /tmp",
            "emptyDir:",
        ):
            if fragment not in document:
                fail(f"{key} is missing observability hardening field: {fragment}")
        if ":latest" in document:
            fail(f"{key} uses latest")
    prometheus = resources[("Deployment", "hooklane-prometheus")]
    if (
        "serviceAccountToken:" not in prometheus
        or "expirationSeconds: 3600" not in prometheus
        or "mountPath: /prometheus" not in prometheus
    ):
        fail("Prometheus projected discovery token or writable TSDB path is missing")
    grafana = resources[("Deployment", "hooklane-grafana")]
    for fragment in (
        "GF_AUTH_ANONYMOUS_ENABLED",
        "GF_AUTH_ANONYMOUS_ORG_ROLE",
        "GF_AUTH_DISABLE_LOGIN_FORM",
        "GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION",
        "mountPath: /var/lib/grafana",
    ):
        if fragment not in grafana:
            fail(f"Grafana local access contract is missing {fragment}")
    role = resources.get(("Role", "hooklane-prometheus-discovery"), "")
    if 'resources: ["pods"]' not in role or 'verbs: ["get", "list", "watch"]' not in role:
        fail("Prometheus discovery Role is not namespace-scoped and read-only")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        default="image",
        choices=("image", "compose", "helm", "observability"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy = load_policy()
    verify_policy_document(policy)
    if args.target == "compose":
        verify_compose()
        print("[ok] Compose four-service runtime hardening policy passed")
        return 0
    if args.target == "helm":
        verify_helm(policy)
        print("[ok] Helm workload hardening and registered limitations passed")
        return 0
    if args.target == "observability":
        verify_observability(policy)
        print("[ok] Prometheus and Grafana runtime hardening policy passed")
        return 0
    verify_application_images()
    print("[ok] current and planned container hardening policy passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"[fail] container policy: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
