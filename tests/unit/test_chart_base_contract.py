from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_OPERATIONS = (
    "kind create cluster",
    "helm install",
    "helm upgrade",
    "kubectl apply",
    "kubectl create",
)


def make_recipe(target: str) -> str:
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{target}:") + 1
    recipe: list[str] = []
    for line in lines[start:]:
        if line and not line.startswith(("\t", " ")):
            break
        recipe.append(line)
    return "\n".join(recipe)


def test_base_validation_targets_do_not_create_or_deploy() -> None:
    inspected = "\n".join(
        (
            make_recipe("kind-config-check"),
            make_recipe("chart-validate-base"),
            (ROOT / "scripts" / "kind_config_check.py").read_text(encoding="utf-8"),
            (ROOT / "scripts" / "chart_validate_base.py").read_text(encoding="utf-8"),
        )
    )
    for operation in FORBIDDEN_OPERATIONS:
        assert operation not in inspected
