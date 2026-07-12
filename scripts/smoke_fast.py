"""Run F001's dependency-free repository and unit-test smoke checks."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DIRS = (
    "src/hooklane/api",
    "src/hooklane/worker",
    "src/hooklane/mock_sink",
    "src/hooklane/domain",
    "src/hooklane/queue",
    "src/hooklane/delivery",
    "src/hooklane/observability",
    "tests/unit",
    "tests/integration",
    "tests/e2e",
    "tests/incidents",
)
EXPECTED_FILES = (
    "Makefile",
    ".python-version",
    "pyproject.toml",
    "requirements.in",
    "requirements.lock",
    "toolchain.toml",
)
REQUIRED_LOCK_PACKAGES = {
    "annotated-doc",
    "annotated-types",
    "anyio",
    "ast-serialize",
    "certifi",
    "click",
    "fastapi",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "iniconfig",
    "librt",
    "mypy",
    "mypy-extensions",
    "packaging",
    "pathspec",
    "pluggy",
    "prometheus-client",
    "pydantic",
    "pydantic-core",
    "pygments",
    "pytest",
    "pytest-asyncio",
    "pyyaml",
    "redis",
    "ruff",
    "setuptools",
    "starlette",
    "typing-extensions",
    "typing-inspection",
    "types-pyyaml",
    "uvicorn",
}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def exact_pins(requirements: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for requirement in requirements:
        if requirement.count("==") != 1 or "latest" in requirement.lower():
            raise ValueError(f"dependency is not exactly pinned: {requirement}")
        name, version = requirement.split("==", 1)
        if not name or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[a-z0-9.-]+)?", version):
            raise ValueError(f"invalid exact dependency pin: {requirement}")
        pins[normalize(name)] = version
    return pins


def load_requirement_pins(filename: str) -> dict[str, str]:
    lines = (ROOT / filename).read_text(encoding="utf-8").splitlines()
    requirements = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return exact_pins(requirements)


def main() -> int:
    errors: list[str] = []
    for relative in EXPECTED_DIRS:
        if not (ROOT / relative).is_dir():
            errors.append(f"missing directory: {relative}")
    for relative in EXPECTED_FILES:
        if not (ROOT / relative).is_file():
            errors.append(f"missing file: {relative}")

    try:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)
        with (ROOT / "toolchain.toml").open("rb") as handle:
            toolchain = tomllib.load(handle)
        print("[ok] configuration: TOML parse passed")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"[fail] configuration: {exc}")
        return 1

    if project["project"]["requires-python"] != "==3.12.*":
        errors.append("pyproject.toml must pin the Python 3.12 minor line")
    configured_python = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    if configured_python != toolchain["python"]["version"]:
        errors.append(".python-version and toolchain.toml disagree")

    declared = [*project["build-system"]["requires"], *project["project"]["dependencies"]]
    for group in project.get("dependency-groups", {}).values():
        declared.extend(group)
    try:
        declared_pins = exact_pins(declared)
        requirement_pins = load_requirement_pins("requirements.in")
        lock_pins = load_requirement_pins("requirements.lock")
        if declared_pins != requirement_pins:
            errors.append("requirements.in does not match pyproject.toml exact pins")
        if set(lock_pins) != REQUIRED_LOCK_PACKAGES:
            errors.append("requirements.lock package set does not match the CPython 3.12 graph")
        for name, version in declared_pins.items():
            if lock_pins.get(name) != version:
                errors.append(f"requirements.lock does not lock {name}=={version}")
    except ValueError as exc:
        errors.append(str(exc))

    version_strings = [
        str(value)
        for section in ("required", "optional")
        for value in toolchain["tools"][section].values()
    ]
    if any("latest" in value.lower() for value in version_strings):
        errors.append("toolchain.toml contains a latest tool version")
    kind_image = str(toolchain["images"]["kind_node"])
    if "@sha256:" not in kind_image or ":latest" in kind_image:
        errors.append("kind node image must be pinned by digest")
    if (ROOT / ".gitmodules").exists():
        errors.append("Git submodules are outside the repository contract")

    for path in (ROOT / "src").rglob("*.py"):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"Python syntax error: {exc}")

    unit = subprocess.run(
        (
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/unit",
            "-p",
            "test_skeleton.py",
        ),
        cwd=ROOT,
        check=False,
        text=True,
    )
    if unit.returncode != 0:
        errors.append(f"minimal unit test exited {unit.returncode}")
    else:
        print("[ok] unit test: repository skeleton import passed")

    if errors:
        for error in errors:
            print(f"[fail] {error}")
        return 1

    print("[ok] repository structure and exact version contracts passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
