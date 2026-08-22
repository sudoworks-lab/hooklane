"""Validate the baseline GitHub Actions workflow without contacting GitHub."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import tomllib
import textwrap
from collections.abc import Mapping
from typing import cast

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACTION_REFERENCE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
CLOUDFLARE = ROOT / "cloudflare"
CONCURRENCY_GROUP = "hooklane-ci-${{ github.workflow }}-${{ github.ref }}"
CHECKOUT_ACTION = "actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8"
SETUP_NODE_ACTION = "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903"
SETUP_PYTHON_ACTION = "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c"
UPLOAD_ARTIFACT_ACTION = (
    "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
)

CODEOWNER = "@sudoworks-lab"
CONTROL_PLANE_PATHS = (
    "/.github/CODEOWNERS",
    "/.github/workflows/**",
    "/Makefile",
    "/scripts/**",
    "/tests/unit/test_ci_toolchain_contract.py",
    "/tests/unit/test_local_image_tag_contract.py",
    "/.python-version",
    "/pyproject.toml",
    "/requirements.lock",
    "/cloudflare/.nvmrc",
    "/cloudflare/.python-version",
    "/cloudflare/package.json",
    "/cloudflare/package-lock.json",
    "/cloudflare/pyproject.toml",
    "/cloudflare/uv.lock",
    "/cloudflare/uv-bootstrap.lock",
    "/cloudflare/harness-requirements.lock",
    "/cloudflare/pylock.toml",
    "/cloudflare/wrangler.jsonc",
)

APPROVED_CONTROL_PLANE_SHA256 = {
    "Makefile": "b4e54f3633c3d8b9ceb57b51876b1f2291887dd46b4d5cc3e17b8f96dde172ac",
    "scripts/ci_setup.py": "26b5dc459f538d0b52d6171633becb9f249d7cfec70088903fed40411fffedcb",
    "scripts/install_ci_tools.py": (
        "7042a3de0733268c9d70de5c2b455956279b00fea1982fe7db40c6cf4a7bbbcc"
    ),
    "scripts/cloudflare_ci_setup.py": (
        "583093f56554f1101352337d19fa8a53c7c79d0ee21458f143bb9d7559ce7c52"
    ),
    "scripts/cloudflare_clean_room.py": (
        "6ac55df67bec40bdf389aa82be64ea6353b50330ed3ff87fd3dc5c6b41c81053"
    ),
    "scripts/cloudflare_local_flow.py": (
        "44b40e1517c4609c7d10ca2c5c2a274d35be90249a8228fc64f74306c4624e15"
    ),
}

CLOUDFLARE_MAKE_TARGETS = {
    "cloudflare-test": (
        "cloudflare-test:",
        "\t@cd cloudflare && $(CLOUDFLARE_UV) lock --check",
        "\t@cd cloudflare && $(CLOUDFLARE_UV) run pytest",
        "\t@cd cloudflare && $(CLOUDFLARE_UV) run ruff check src tests",
        "\t@cd cloudflare && $(CLOUDFLARE_UV) run mypy src",
    ),
    "cloudflare-local-flow": (
        "cloudflare-local-flow:",
        "\t@test -n \"$(CLOUDFLARE_HARNESS_PYTHON)\" || { echo \"[fail] Python: no Cloudflare flow interpreter was found\"; exit 1; }",
        "\t@HOOKLANE_CLOUDFLARE_UV=\"$(CLOUDFLARE_UV)\" \\",
        "\t\t$(CLOUDFLARE_HARNESS_PYTHON) scripts/cloudflare_local_flow.py",
    ),
    "cloudflare-check": ("cloudflare-check: cloudflare-test cloudflare-local-flow",),
    "cloudflare-ci-setup": (
        "cloudflare-ci-setup:",
        "\t@test -n \"$(PYTHON)\" || { echo \"[fail] Python: no Cloudflare CI bootstrap interpreter was found\"; exit 1; }",
        "\t@$(PYTHON) scripts/cloudflare_ci_setup.py",
    ),
    "cloudflare-ci-check": (
        "cloudflare-ci-check: cloudflare-ci-setup",
        "\t@$(MAKE) cloudflare-check \\",
        "\t\tCLOUDFLARE_UV=\"$(CLOUDFLARE_CI_UV)\" \\",
        "\t\tCLOUDFLARE_HARNESS_PYTHON=\"$(CLOUDFLARE_CI_HARNESS_PYTHON)\"",
    ),
    "cloudflare-clean-room": (
        "cloudflare-clean-room:",
        "\t@test -n \"$(PYTHON)\" || { echo \"[fail] Python: no clean-room interpreter was found\"; exit 1; }",
        "\t@$(PYTHON) scripts/cloudflare_clean_room.py",
    ),
}

QUALITY_BOOTSTRAP_RUN = r'''export PATH="$HOME/.local/bin:$PATH"
make ci-setup
terraform version
printf '%s\n' "$HOME/.local/bin" >> "$GITHUB_PATH"'''
IMAGE_IDENTITY_RUN = r'''SOURCE_SHA="$GITHUB_SHA"
if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[fail] source commit SHA must be 40 lowercase hexadecimal characters"
  exit 1
fi
IMAGE_TAG="git-$SOURCE_SHA"
printf 'IMAGE_TAG=%s\n' "$IMAGE_TAG" >> "$GITHUB_ENV"'''
QUALITY_GATE_RUN = r'''make images-build IMAGE_TAG="$IMAGE_TAG"
make observability-images
make image-contract IMAGE_TAG="$IMAGE_TAG"
make verify IMAGE_TAG="$IMAGE_TAG"'''
E2E_BOOTSTRAP_RUN = r'''make ci-setup
printf '%s\n' "$HOME/.local/bin" >> "$GITHUB_PATH"'''
E2E_GATE_RUN = r'''make e2e-kind IMAGE_TAG="$IMAGE_TAG"'''
CLEANUP_RUN = "make cluster-down"
CLOUDFLARE_BOOTSTRAP_RUN = "make cloudflare-ci-setup"
CLOUDFLARE_GATE_RUN = r'''make cloudflare-check \
  CLOUDFLARE_UV="$PWD/cloudflare/.venv-uv/bin/uv" \
  CLOUDFLARE_HARNESS_PYTHON="$PWD/cloudflare/.venv-harness/bin/python"'''


class ContractError(RuntimeError):
    """The local workflow contract is unsafe or incomplete."""


def object_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ContractError(f"{label} must be a mapping")
    return cast(dict[str, object], value)


def object_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a list")
    return cast(list[object], value)


def allow_keys(
    mapping: dict[str, object], label: str, allowed: set[str], *, required: set[str] | None = None
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        if "if" in unknown or "continue-on-error" in unknown:
            raise ContractError(f"{label} must not be skipped or weakened")
        raise ContractError(f"{label} contains unexpected keys: {', '.join(unknown)}")
    missing = sorted((required or set()) - set(mapping))
    if missing:
        raise ContractError(f"{label} is missing required keys: {', '.join(missing)}")


def canonical_run_body(run: object, label: str) -> str:
    if not isinstance(run, str):
        raise ContractError(f"{label} must run a shell command")
    normalized = textwrap.dedent(run.replace("\r\n", "\n")).strip()
    return "\n".join(line.rstrip() for line in normalized.splitlines())


def validate_run_step(
    step: dict[str, object],
    label: str,
    *,
    name: str,
    expected_run: str,
    expected_env: Mapping[str, object] | None = None,
    marker: str | None = None,
    expected_if: str | None = None,
) -> None:
    expected_keys = {"name", "run"}
    if expected_env is not None:
        expected_keys.add("env")
    if expected_if is not None:
        expected_keys.add("if")
    allow_keys(step, label, expected_keys, required=expected_keys)
    if step.get("name") != name:
        raise ContractError(f"{label} has an unexpected step name")
    if expected_env is not None:
        environment = object_dict(step.get("env"), f"{label} environment")
        if environment != expected_env:
            raise ContractError(f"{label} environment is outside the allowlist")
    if expected_if is not None and step.get("if") != expected_if:
        raise ContractError(f"{label} has an unexpected step-level if condition")
    if canonical_run_body(step.get("run"), label) != canonical_run_body(expected_run, label):
        suffix = f" ({marker})" if marker is not None else ""
        raise ContractError(f"{label} must execute the exact run body{suffix}")


def validate_uses_step(
    step: dict[str, object],
    label: str,
    *,
    name: str,
    approved_reference: str,
    options: dict[str, object],
    expected_if: str | None = None,
) -> None:
    expected_keys = {"name", "uses", "with"}
    if expected_if is not None:
        expected_keys.add("if")
    allow_keys(step, label, expected_keys, required=expected_keys)
    if step.get("name") != name:
        raise ContractError(f"{label} has an unexpected step name")
    uses = step.get("uses")
    if not isinstance(uses, str) or not ACTION_REFERENCE.fullmatch(uses):
        raise ContractError(f"{label} action reference is not a full commit SHA")
    if uses != approved_reference:
        raise ContractError(f"{label} must use the exact approved action revision")
    if object_dict(step.get("with"), f"{label} options") != options:
        raise ContractError(f"{label} action options are outside the allowlist")
    if expected_if is not None and step.get("if") != expected_if:
        raise ContractError(f"{label} has an unexpected step-level if condition")


def make_target_block(makefile: str, target: str) -> str:
    lines = makefile.splitlines()
    header = f"{target}:"
    matches = tuple(
        index for index, line in enumerate(lines) if line == header or line.startswith(f"{header} ")
    )
    if len(matches) != 1:
        raise ContractError(f"Cloudflare Make target must be defined exactly once: {target}")
    start = matches[0]
    end = start + 1
    while end < len(lines) and lines[end].startswith("\t"):
        end += 1
    return "\n".join(lines[start:end])


def validate_codeowners_text(source: str) -> None:
    lines = tuple(
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    expected = tuple(f"{path} {CODEOWNER}" for path in CONTROL_PLANE_PATHS)
    if lines != expected:
        raise ContractError(
            "CODEOWNERS must exactly cover the CI control-plane paths and itself"
        )


def validate_codeowners_contract() -> None:
    codeowners = ROOT / ".github" / "CODEOWNERS"
    validate_codeowners_text(codeowners.read_text(encoding="utf-8"))


def validate_approved_control_plane_sources(sources: Mapping[str, str]) -> None:
    if set(sources) != set(APPROVED_CONTROL_PLANE_SHA256):
        raise ContractError("approved CI bootstrap source set is incomplete")
    for relative_path, source in sources.items():
        actual = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if actual != APPROVED_CONTROL_PLANE_SHA256[relative_path]:
            raise ContractError(f"CI control-plane source changed without approval: {relative_path}")


def validate_cloudflare_make_contract(makefile: str) -> tuple[str, ...]:
    blocks: list[str] = []
    for target, expected_lines in CLOUDFLARE_MAKE_TARGETS.items():
        block = make_target_block(makefile, target)
        if block != "\n".join(expected_lines):
            raise ContractError(f"Cloudflare Make target changed execution semantics: {target}")
        blocks.append(block)
    logical_makefile = makefile.replace("\\\n", " ")
    phony_targets = {
        target
        for line in logical_makefile.splitlines()
        if line.startswith(".PHONY:")
        for target in line.removeprefix(".PHONY:").split()
    }
    if not set(CLOUDFLARE_MAKE_TARGETS).issubset(phony_targets):
        raise ContractError("all Cloudflare CI targets must remain phony")
    return tuple(blocks)


def reject_job_if(job: dict[str, object], label: str) -> None:
    if "if" in job:
        raise ContractError(f"{label} must not define a job-level if condition")


def reject_job_continue_on_error(job: dict[str, object], label: str) -> None:
    if "continue-on-error" in job:
        raise ContractError(f"{label} must not define continue-on-error")


def parse_workflow() -> tuple[dict[str, object], str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    try:
        document: object = yaml.load(text, Loader=yaml.BaseLoader)
    except yaml.YAMLError as error:
        raise ContractError("workflow YAML could not be parsed") from error
    return object_dict(document, "workflow"), text


def validate_events(workflow: dict[str, object]) -> None:
    events = object_dict(workflow.get("on"), "workflow triggers")
    allow_keys(events, "workflow triggers", {"pull_request", "push"}, required={"pull_request", "push"})
    if set(events) != {"pull_request", "push"}:
        raise ContractError("workflow must target pull_request and push only")
    if events.get("pull_request") not in (None, ""):
        raise ContractError("pull_request trigger must use the default event contract")
    push = object_dict(events.get("push"), "push trigger")
    allow_keys(push, "push trigger", {"branches"}, required={"branches"})
    branches = object_list(push.get("branches"), "push branches")
    if branches != ["main"]:
        raise ContractError("push trigger must target main only")


def validate_permissions_and_concurrency(workflow: dict[str, object]) -> None:
    if "env" in workflow:
        raise ContractError("workflow must not define a top-level environment")
    allow_keys(
        workflow,
        "workflow",
        {"name", "on", "permissions", "concurrency", "defaults", "jobs"},
        required={"name", "on", "permissions", "concurrency", "defaults", "jobs"},
    )
    if workflow.get("name") != "Hooklane CI":
        raise ContractError("workflow name is outside the allowlist")
    permissions = object_dict(workflow.get("permissions"), "workflow permissions")
    allow_keys(permissions, "workflow permissions", {"contents"}, required={"contents"})
    if permissions != {"contents": "read"}:
        raise ContractError("workflow permissions must be contents: read only")
    concurrency = object_dict(workflow.get("concurrency"), "workflow concurrency")
    allow_keys(
        concurrency,
        "workflow concurrency",
        {"group", "cancel-in-progress"},
        required={"group", "cancel-in-progress"},
    )
    if concurrency.get("group") != CONCURRENCY_GROUP:
        raise ContractError("concurrency group must match the canonical workflow/ref expression")
    if concurrency.get("cancel-in-progress") != "true":
        raise ContractError("concurrency must cancel superseded runs")
    defaults = object_dict(workflow.get("defaults"), "workflow defaults")
    allow_keys(defaults, "workflow defaults", {"run"}, required={"run"})
    run_defaults = object_dict(defaults.get("run"), "run defaults")
    allow_keys(run_defaults, "run defaults", {"shell"}, required={"shell"})
    if run_defaults.get("shell") != "bash":
        raise ContractError("workflow shell must be bash")


def validate_job_set(workflow: dict[str, object]) -> None:
    jobs = object_dict(workflow.get("jobs"), "workflow jobs")
    if set(jobs) != {"quality", "cloudflare", "e2e-kind"}:
        raise ContractError("workflow must contain quality, Cloudflare, and kind E2E jobs")


def validate_quality_job(workflow: dict[str, object]) -> None:
    jobs = object_dict(workflow.get("jobs"), "workflow jobs")
    quality = object_dict(jobs.get("quality"), "quality job")
    reject_job_if(quality, "quality job")
    reject_job_continue_on_error(quality, "quality job")
    if "permissions" in quality:
        raise ContractError("quality job must not expand top-level permissions")
    allow_keys(
        quality,
        "quality job",
        {"name", "runs-on", "timeout-minutes", "env", "steps"},
        required={"name", "runs-on", "timeout-minutes", "env", "steps"},
    )
    if quality.get("name") != "Quality, security, and chart gates":
        raise ContractError("quality job name is outside the allowlist")
    if quality.get("runs-on") != "ubuntu-24.04":
        raise ContractError("quality job must use the fixed Ubuntu runner label")
    timeout = quality.get("timeout-minutes")
    if not isinstance(timeout, str) or not timeout.isdigit() or int(timeout) > 40:
        raise ContractError("quality job requires a bounded timeout")
    quality_environment = object_dict(quality.get("env"), "quality environment")
    if quality_environment != {"HOOKLANE_TERRAFORM_REQUIRED": "1"}:
        raise ContractError("quality job must require Terraform instead of allowing a skip")

    steps = object_list(quality.get("steps"), "quality steps")
    if len(steps) != 5:
        raise ContractError("quality job must contain the exact five-step sequence")
    validate_uses_step(
        object_dict(steps[0], "quality checkout step"),
        "quality checkout step",
        name="Check out repository history",
        approved_reference=CHECKOUT_ACTION,
        options={"fetch-depth": "0", "persist-credentials": "false"},
    )
    validate_uses_step(
        object_dict(steps[1], "quality Python step"),
        "quality Python step",
        name="Set up pinned Python",
        approved_reference=SETUP_PYTHON_ACTION,
        options={"python-version-file": ".python-version", "check-latest": "false"},
    )
    validate_run_step(
        object_dict(steps[2], "quality bootstrap step"),
        "quality bootstrap step",
        name="Bootstrap pinned dependencies and tools",
        expected_run=QUALITY_BOOTSTRAP_RUN,
        marker="make ci-setup",
    )
    validate_run_step(
        object_dict(steps[3], "quality image identity step"),
        "quality image identity step",
        name="Determine checked-out source image identity",
        expected_run=IMAGE_IDENTITY_RUN,
    )
    validate_run_step(
        object_dict(steps[4], "quality gate step"),
        "quality gate step",
        name="Run local quality, security, and chart gates",
        expected_run=QUALITY_GATE_RUN,
        marker="make verify",
    )


def validate_cloudflare_bootstrap_contract() -> None:
    root_python = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    cloudflare_python = (CLOUDFLARE / ".python-version").read_text(encoding="utf-8").strip()
    if root_python != "3.12.3":
        raise ContractError("root harness Python must remain pinned to 3.12.3")
    if cloudflare_python != "3.13":
        raise ContractError("Cloudflare Python must remain isolated on the 3.13 series")

    package = object_dict(
        json.loads((CLOUDFLARE / "package.json").read_text(encoding="utf-8")),
        "Cloudflare package",
    )
    engines = object_dict(package.get("engines"), "Cloudflare Node engines")
    dependencies = object_dict(package.get("devDependencies"), "Cloudflare Node dependencies")
    node_version = (CLOUDFLARE / ".nvmrc").read_text(encoding="utf-8").strip()
    if node_version != "22.22.2" or engines.get("node") != node_version:
        raise ContractError("Cloudflare Node version must come from the matching .nvmrc pin")
    if dependencies.get("wrangler") != "4.124.0":
        raise ContractError("Wrangler must remain exactly pinned to 4.124.0")

    project = object_dict(
        tomllib.loads((CLOUDFLARE / "pyproject.toml").read_text(encoding="utf-8")),
        "Cloudflare Python project",
    )
    project_metadata = object_dict(project.get("project"), "Cloudflare project metadata")
    dependency_groups = object_dict(project.get("dependency-groups"), "dependency groups")
    dev_dependencies = object_list(dependency_groups.get("dev"), "Cloudflare dev dependencies")
    if project_metadata.get("requires-python") != "==3.13.*":
        raise ContractError("Cloudflare project must require a separate Python 3.13 runtime")
    if "uv==0.12.3" not in dev_dependencies:
        raise ContractError("Cloudflare project must retain the uv 0.12.3 pin")

    uv_bootstrap = (CLOUDFLARE / "uv-bootstrap.lock").read_text(encoding="utf-8")
    if "uv==0.12.3" not in uv_bootstrap or not re.search(
        r"--hash=sha256:[0-9a-f]{64}\b", uv_bootstrap
    ):
        raise ContractError("uv bootstrap must use version 0.12.3 with a SHA-256 hash")

    root_lock = set((ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines())
    harness_requirements = {
        line
        for line in (CLOUDFLARE / "harness-requirements.lock")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("#")
    }
    if not harness_requirements or not harness_requirements.issubset(root_lock):
        raise ContractError("root mock-sink harness must be a pinned subset of requirements.lock")

    root_setup = (ROOT / "scripts" / "ci_setup.py").read_text(encoding="utf-8")
    tool_installer = (ROOT / "scripts" / "install_ci_tools.py").read_text(encoding="utf-8")
    setup = (ROOT / "scripts" / "cloudflare_ci_setup.py").read_text(encoding="utf-8")
    clean_room = (ROOT / "scripts" / "cloudflare_clean_room.py").read_text(encoding="utf-8")
    local_flow = (ROOT / "scripts" / "cloudflare_local_flow.py").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    validate_approved_control_plane_sources(
        {
            "Makefile": makefile,
            "scripts/ci_setup.py": root_setup,
            "scripts/install_ci_tools.py": tool_installer,
            "scripts/cloudflare_ci_setup.py": setup,
            "scripts/cloudflare_clean_room.py": clean_room,
            "scripts/cloudflare_local_flow.py": local_flow,
        }
    )
    if "GITHUB_PATH" in setup:
        raise ContractError("Cloudflare bootstrap must not modify the next step's PATH")
    for marker in (
        'UV_VERSION = "0.12.3"',
        '"--require-hashes"',
        '"harness-requirements.lock"',
        '"python", "install", cloudflare_python_version',
        '"--locked"',
    ):
        if marker not in setup:
            raise ContractError(f"Cloudflare bootstrap contract is missing: {marker}")
    for marker in (
        '"WRANGLER_SEND_METRICS": "false"',
        '"HOME": str(tool_home)',
    ):
        if marker not in local_flow:
            raise ContractError(f"Cloudflare local isolation contract is missing: {marker}")
    make_blocks = validate_cloudflare_make_contract(makefile)
    executable_surface = "\n".join((setup, clean_room, local_flow, *make_blocks)).lower()
    for marker in (
        "wrangler deploy",
        "wrangler publish",
        "git push",
        "docker push",
        "--remote",
        "cloudflare_api_token",
        "cloudflare_api_key",
        "cloudflare_account_id",
        "cloudflare_email",
    ):
        if marker in executable_surface:
            raise ContractError(f"Cloudflare executable surface contains prohibited content: {marker}")


def validate_cloudflare_job(workflow: dict[str, object]) -> None:
    jobs = object_dict(workflow.get("jobs"), "workflow jobs")
    cloudflare = object_dict(jobs.get("cloudflare"), "Cloudflare job")
    reject_job_if(cloudflare, "Cloudflare job")
    reject_job_continue_on_error(cloudflare, "Cloudflare job")
    if "needs" in cloudflare:
        raise ContractError("Cloudflare gate must run independently of the quality job")
    if "env" in cloudflare:
        raise ContractError("Cloudflare job must not require environment credentials")
    if "permissions" in cloudflare:
        raise ContractError("Cloudflare job must not expand top-level permissions")
    allow_keys(
        cloudflare,
        "Cloudflare job",
        {"name", "runs-on", "timeout-minutes", "steps"},
        required={"name", "runs-on", "timeout-minutes", "steps"},
    )
    if cloudflare.get("name") != "Cloudflare local backend gate":
        raise ContractError("Cloudflare job name is outside the allowlist")
    if cloudflare.get("runs-on") != "ubuntu-24.04":
        raise ContractError("Cloudflare job must use the fixed Ubuntu runner label")
    timeout = cloudflare.get("timeout-minutes")
    if not isinstance(timeout, str) or not timeout.isdigit() or not 0 < int(timeout) <= 25:
        raise ContractError("Cloudflare job requires a bounded timeout")
    steps = object_list(cloudflare.get("steps"), "Cloudflare steps")
    if len(steps) != 5:
        raise ContractError("Cloudflare job must use the exact action sequence and step sequence")
    validate_uses_step(
        object_dict(steps[0], "Cloudflare checkout step"),
        "Cloudflare checkout step",
        name="Check out repository",
        approved_reference=CHECKOUT_ACTION,
        options={"persist-credentials": "false"},
    )
    validate_uses_step(
        object_dict(steps[1], "Cloudflare Node step"),
        "Cloudflare Node step",
        name="Set up pinned Node.js",
        approved_reference=SETUP_NODE_ACTION,
        options={
            "node-version-file": "cloudflare/.nvmrc",
            "package-manager-cache": "false",
        },
    )
    validate_uses_step(
        object_dict(steps[2], "Cloudflare Python step"),
        "Cloudflare Python step",
        name="Set up root harness Python",
        approved_reference=SETUP_PYTHON_ACTION,
        options={"python-version-file": ".python-version", "check-latest": "false"},
    )
    validate_run_step(
        object_dict(steps[3], "Cloudflare bootstrap step"),
        "Cloudflare bootstrap step",
        name="Bootstrap isolated pinned environments",
        expected_run=CLOUDFLARE_BOOTSTRAP_RUN,
        marker="make cloudflare-ci-setup",
    )
    validate_run_step(
        object_dict(steps[4], "Cloudflare gate step"),
        "Cloudflare gate step",
        name="Run Cloudflare local backend gate",
        expected_run=CLOUDFLARE_GATE_RUN,
        marker="make cloudflare-check",
    )
    validate_cloudflare_bootstrap_contract()


def validate_e2e_job(workflow: dict[str, object]) -> None:
    jobs = object_dict(workflow.get("jobs"), "workflow jobs")
    e2e = object_dict(jobs.get("e2e-kind"), "kind E2E job")
    reject_job_if(e2e, "kind E2E job")
    reject_job_continue_on_error(e2e, "kind E2E job")
    if "permissions" in e2e:
        raise ContractError("kind E2E must not expand top-level permissions")
    allow_keys(
        e2e,
        "kind E2E job",
        {"name", "needs", "runs-on", "timeout-minutes", "steps"},
        required={"name", "needs", "runs-on", "timeout-minutes", "steps"},
    )
    if e2e.get("name") != "kind delivery and recovery E2E":
        raise ContractError("kind E2E job name is outside the allowlist")
    if e2e.get("needs") != ["quality", "cloudflare"]:
        raise ContractError("kind E2E must run only after quality and Cloudflare gates")
    if e2e.get("runs-on") != "ubuntu-24.04":
        raise ContractError("kind E2E must use the fixed Ubuntu runner label")
    timeout = e2e.get("timeout-minutes")
    if not isinstance(timeout, str) or not timeout.isdigit() or int(timeout) > 30:
        raise ContractError("kind E2E requires a bounded timeout")

    steps = object_list(e2e.get("steps"), "kind E2E steps")
    if len(steps) != 7:
        raise ContractError("kind E2E job must contain the exact seven-step sequence")
    validate_uses_step(
        object_dict(steps[0], "kind E2E checkout step"),
        "kind E2E checkout step",
        name="Check out repository history",
        approved_reference=CHECKOUT_ACTION,
        options={"fetch-depth": "0", "persist-credentials": "false"},
    )
    validate_uses_step(
        object_dict(steps[1], "kind E2E Python step"),
        "kind E2E Python step",
        name="Set up pinned Python",
        approved_reference=SETUP_PYTHON_ACTION,
        options={"python-version-file": ".python-version", "check-latest": "false"},
    )
    validate_run_step(
        object_dict(steps[2], "kind E2E bootstrap step"),
        "kind E2E bootstrap step",
        name="Bootstrap pinned dependencies and tools",
        expected_run=E2E_BOOTSTRAP_RUN,
        marker="make ci-setup",
    )
    validate_run_step(
        object_dict(steps[3], "kind E2E image identity step"),
        "kind E2E image identity step",
        name="Determine checked-out source image identity",
        expected_run=IMAGE_IDENTITY_RUN,
    )
    validate_run_step(
        object_dict(steps[4], "kind E2E gate step"),
        "kind E2E gate step",
        name="Run local kind E2E",
        expected_run=E2E_GATE_RUN,
        marker="make e2e-kind",
    )
    validate_uses_step(
        object_dict(steps[5], "kind E2E diagnostics step"),
        "kind E2E diagnostics step",
        name="Upload sanitized failure diagnostics",
        approved_reference=UPLOAD_ARTIFACT_ACTION,
        options={
            "name": "hooklane-kind-e2e-diagnostics",
            "path": "artifacts/kind-e2e-*",
            "if-no-files-found": "error",
            "retention-days": "3",
        },
        expected_if="failure()",
    )
    validate_run_step(
        object_dict(steps[6], "kind E2E cleanup step"),
        "kind E2E cleanup step",
        name="Clean up project kind cluster",
        expected_run=CLEANUP_RUN,
        marker="make cluster-down",
        expected_if="always()",
    )


def validate_prohibited_content(text: str) -> None:
    prohibited = (
        "pull_request_target",
        "${{ secrets.",
        "git push",
        "docker push",
        "wrangler deploy",
        "wrangler publish",
        "--remote",
        "helm install",
        ":latest",
        "0.1.1",
    )
    for marker in prohibited:
        if marker in text:
            raise ContractError(f"workflow contains prohibited content: {marker}")
    if re.search(r"/(?:home|Users)/[^/\s]+/", text):
        raise ContractError("workflow contains a personal absolute path")


def main() -> int:
    try:
        workflow, text = parse_workflow()
        validate_events(workflow)
        validate_permissions_and_concurrency(workflow)
        validate_job_set(workflow)
        validate_quality_job(workflow)
        validate_cloudflare_job(workflow)
        validate_e2e_job(workflow)
        validate_prohibited_content(text)
        validate_codeowners_contract()
    except (OSError, ContractError) as error:
        print(f"[fail] CI contract: {error}")
        return 1
    print(
        "[ok] CI YAML, three required jobs, exact action SHA pins, source identity, "
        "approved bootstrap sources, exact Cloudflare Make targets, CODEOWNERS, and "
        "untrusted-input contract passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
