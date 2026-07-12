"""Lint and render the base chart without contacting a Kubernetes cluster."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Never


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "hooklane"
KUBE_VERSION = "1.34.8"
REQUIRED_TEMPLATES = {
    "api-deployment.yaml",
    "api-service.yaml",
    "worker-deployment.yaml",
    "mock-sink-deployment.yaml",
    "mock-sink-service.yaml",
    "redis-statefulset.yaml",
    "redis-service.yaml",
    "configmap.yaml",
    "secret.yaml",
    "serviceaccount.yaml",
    "pdb.yaml",
    "tests/smoke-test.yaml",
}
REQUIRED_RESOURCES = {
    ("Deployment", "hooklane-api"),
    ("Deployment", "hooklane-worker"),
    ("Deployment", "hooklane-mock-sink"),
    ("StatefulSet", "hooklane-redis"),
    ("Service", "hooklane-api"),
    ("Service", "hooklane-mock-sink"),
    ("Service", "hooklane-redis"),
    ("ConfigMap", "hooklane-config"),
    ("Secret", "hooklane-placeholder"),
    ("ServiceAccount", "hooklane"),
    ("PodDisruptionBudget", "hooklane-api"),
    ("Pod", "hooklane-test"),
}


def fail(message: str) -> Never:
    raise RuntimeError(message)


def run_helm(*args: str) -> str:
    completed = subprocess.run(
        ["helm", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def rendered_resources(rendered: str) -> dict[tuple[str, str], str]:
    resources: dict[tuple[str, str], str] = {}
    for document in re.split(r"^---\s*$", rendered, flags=re.MULTILINE):
        kind_match = re.search(r"^kind:\s*(\S+)\s*$", document, flags=re.MULTILINE)
        name_match = re.search(
            r"^metadata:\s*\n(?:^[ \t]+.*\n)*?^[ \t]+name:\s*(\S+)\s*$",
            document,
            flags=re.MULTILINE,
        )
        if kind_match and name_match:
            key = (kind_match.group(1), name_match.group(1))
            if key in resources:
                fail(f"render contains duplicate resource: {key}")
            resources[key] = document
    return resources


def main() -> int:
    schema: object = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
    if not isinstance(schema, dict) or schema.get("$schema") is None:
        fail("values.schema.json is not a JSON schema object")

    templates = {
        str(path.relative_to(CHART / "templates"))
        for path in (CHART / "templates").rglob("*.yaml")
    }
    missing_templates = sorted(REQUIRED_TEMPLATES - templates)
    if missing_templates:
        fail(f"chart is missing templates: {', '.join(missing_templates)}")

    run_helm("lint", str(CHART), "--strict")
    rendered = run_helm(
        "template",
        "hooklane",
        str(CHART),
        "--namespace",
        "hooklane",
        "--kube-version",
        KUBE_VERSION,
    )
    resources = rendered_resources(rendered)
    missing_resources = sorted(REQUIRED_RESOURCES - set(resources))
    if missing_resources:
        fail(f"render is missing resources: {missing_resources}")
    if ":latest" in rendered or ":latest" in (CHART / "values.yaml").read_text(encoding="utf-8"):
        fail("rendered chart must not use latest")
    secret = resources[("Secret", "hooklane-placeholder")]
    if "data: {}" not in secret or "stringData:" in secret:
        fail("placeholder Secret must contain no values")
    test_pod = resources[("Pod", "hooklane-test")]
    if '"helm.sh/hook": test' not in test_pod:
        fail("Helm test skeleton is not marked as a test hook")
    print("[ok] Helm lint, schema, render, required resources, and empty Secret passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (json.JSONDecodeError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[fail] base chart: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
