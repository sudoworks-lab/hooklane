from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import pytest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = (ROOT / "scripts" / "install_ci_tools.py").read_text(encoding="utf-8")


def _ci_contract() -> ModuleType:
    path = ROOT / "scripts" / "ci_contract.py"
    spec = importlib.util.spec_from_file_location("hooklane_ci_contract_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ci_contract = _ci_contract()


def _jobs(workflow: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], ci_contract.object_dict(workflow.get("jobs"), "workflow jobs"))


def _cloudflare_steps(workflow: dict[str, object]) -> list[object]:
    cloudflare = ci_contract.object_dict(_jobs(workflow).get("cloudflare"), "Cloudflare job")
    return cast(list[object], ci_contract.object_list(cloudflare.get("steps"), "Cloudflare steps"))


def _cloudflare_step(workflow: dict[str, object], name: str) -> dict[str, object]:
    for raw_step in _cloudflare_steps(workflow):
        step = ci_contract.object_dict(raw_step, "Cloudflare step")
        if step.get("name") == name:
            return cast(dict[str, object], step)
    raise AssertionError(f"Cloudflare step not found: {name}")


def _job_steps(workflow: dict[str, object], job_name: str) -> list[object]:
    job = ci_contract.object_dict(_jobs(workflow).get(job_name), f"{job_name} job")
    return cast(list[object], ci_contract.object_list(job.get("steps"), f"{job_name} steps"))


def _job_step(workflow: dict[str, object], job_name: str, index: int) -> dict[str, object]:
    return cast(
        dict[str, object],
        ci_contract.object_dict(_job_steps(workflow, job_name)[index], f"{job_name} step"),
    )


def test_terraform_ci_tool_is_exact_and_checksum_pinned() -> None:
    assert 'name="terraform"' in INSTALLER
    assert 'version="1.15.5"' in INSTALLER
    assert 'asset="terraform_1.15.5_linux_amd64.zip"' in INSTALLER
    assert '"https://releases.hashicorp.com/terraform/1.15.5/"' in INSTALLER
    assert '"terraform_1.15.5_linux_amd64.zip"' in INSTALLER
    assert '"terraform_1.15.5_SHA256SUMS"' in INSTALLER
    assert 'archive_format="zip"' in INSTALLER
    assert 'archive_member="terraform"' in INSTALLER
    assert 'BIN_DIR = Path.home() / ".local" / "bin"' in INSTALLER


def test_ci_workflow_requires_pinned_terraform_verification() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    assert 'HOOKLANE_TERRAFORM_REQUIRED: "1"' in workflow
    assert 'export PATH="$HOME/.local/bin:$PATH"' in workflow
    assert "terraform version" in workflow


def test_ci_contract_rejects_removed_cloudflare_job() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    jobs = ci_contract.object_dict(mutated.get("jobs"), "workflow jobs")
    del jobs["cloudflare"]

    with pytest.raises(ci_contract.ContractError, match="Cloudflare"):
        ci_contract.validate_job_set(mutated)


def test_ci_contract_rejects_weakened_cloudflare_command() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    jobs = ci_contract.object_dict(mutated.get("jobs"), "workflow jobs")
    cloudflare = ci_contract.object_dict(jobs.get("cloudflare"), "Cloudflare job")
    steps = ci_contract.object_list(cloudflare.get("steps"), "Cloudflare steps")
    for raw_step in steps:
        step = ci_contract.object_dict(raw_step, "Cloudflare step")
        run = step.get("run")
        if isinstance(run, str) and "make cloudflare-check" in run:
            step["run"] = run.replace("make cloudflare-check", "make cloudflare-test")

    with pytest.raises(ci_contract.ContractError, match="make cloudflare-check"):
        ci_contract.validate_cloudflare_job(mutated)


def test_ci_contract_rejects_skipped_cloudflare_gate() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    jobs = ci_contract.object_dict(mutated.get("jobs"), "workflow jobs")
    cloudflare = ci_contract.object_dict(jobs.get("cloudflare"), "Cloudflare job")
    steps = ci_contract.object_list(cloudflare.get("steps"), "Cloudflare steps")
    final_step = ci_contract.object_dict(steps[-1], "Cloudflare final step")
    final_step["continue-on-error"] = "true"

    with pytest.raises(ci_contract.ContractError, match="skipped or weakened"):
        ci_contract.validate_cloudflare_job(mutated)


def test_ci_contract_requires_both_kind_preflight_gates() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    jobs = ci_contract.object_dict(mutated.get("jobs"), "workflow jobs")
    e2e = ci_contract.object_dict(jobs.get("e2e-kind"), "kind E2E job")
    e2e["needs"] = ["quality"]

    with pytest.raises(ci_contract.ContractError, match="quality and Cloudflare"):
        ci_contract.validate_e2e_job(mutated)


def test_all_checkout_steps_disable_credential_persistence() -> None:
    workflow, _text = ci_contract.parse_workflow()
    jobs = _jobs(workflow)
    for job_name in ("quality", "cloudflare", "e2e-kind"):
        job = ci_contract.object_dict(jobs[job_name], f"{job_name} job")
        steps = ci_contract.object_list(job.get("steps"), f"{job_name} steps")
        checkout_steps = [
            ci_contract.object_dict(raw_step, f"{job_name} step")
            for raw_step in steps
            if str(ci_contract.object_dict(raw_step, f"{job_name} step").get("uses", "")).startswith(
                "actions/checkout@"
            )
        ]
        assert len(checkout_steps) == 1
        options = ci_contract.object_dict(checkout_steps[0].get("with"), "checkout options")
        assert options.get("persist-credentials") == "false"


def test_ci_contract_rejects_workflow_level_environment() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    mutated["env"] = {"CI_MARKER": "placeholder"}

    with pytest.raises(ci_contract.ContractError, match="top-level environment"):
        ci_contract.validate_permissions_and_concurrency(mutated)


@pytest.mark.parametrize(
    ("job_name", "validator"),
    (
        ("quality", ci_contract.validate_quality_job),
        ("cloudflare", ci_contract.validate_cloudflare_job),
        ("e2e-kind", ci_contract.validate_e2e_job),
    ),
    ids=("quality", "cloudflare", "e2e-kind"),
)
def test_ci_contract_rejects_job_level_if(
    job_name: str, validator: object,
) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    ci_contract.object_dict(_jobs(mutated)[job_name], f"{job_name} job")["if"] = "false"

    assert callable(validator)
    with pytest.raises(ci_contract.ContractError, match="job-level if"):
        validator(mutated)


@pytest.mark.parametrize(
    ("job_name", "validator"),
    (
        ("quality", ci_contract.validate_quality_job),
        ("cloudflare", ci_contract.validate_cloudflare_job),
        ("e2e-kind", ci_contract.validate_e2e_job),
    ),
    ids=("quality", "cloudflare", "e2e-kind"),
)
def test_ci_contract_rejects_job_level_continue_on_error(
    job_name: str, validator: object,
) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    ci_contract.object_dict(_jobs(mutated)[job_name], f"{job_name} job")[
        "continue-on-error"
    ] = "true"

    assert callable(validator)
    with pytest.raises(ci_contract.ContractError, match="continue-on-error"):
        validator(mutated)


def test_ci_contract_rejects_echo_cloudflare_bootstrap() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    _cloudflare_step(mutated, "Bootstrap isolated pinned environments")["run"] = (
        "echo make cloudflare-ci-setup"
    )

    with pytest.raises(ci_contract.ContractError, match="bootstrap step"):
        ci_contract.validate_cloudflare_job(mutated)


@pytest.mark.parametrize(
    "run",
    (
        "echo 'make cloudflare-check CLOUDFLARE_UV= CLOUDFLARE_HARNESS_PYTHON='",
        "# make cloudflare-check\n# CLOUDFLARE_UV= CLOUDFLARE_HARNESS_PYTHON=",
        "true",
    ),
    ids=("echo", "comment", "deleted"),
)
def test_ci_contract_rejects_nonexecuting_cloudflare_gate(run: str) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    _cloudflare_step(mutated, "Run Cloudflare local backend gate")["run"] = run

    with pytest.raises(ci_contract.ContractError, match="gate step"):
        ci_contract.validate_cloudflare_job(mutated)


@pytest.mark.parametrize("unsafe", (False, True), ids=("duplicate", "unsafe-duplicate"))
def test_ci_contract_rejects_extra_checkout(unsafe: bool) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    steps = _cloudflare_steps(mutated)
    original = ci_contract.object_dict(steps[0], "checkout step")
    if unsafe:
        extra: dict[str, object] = {
            "name": "Additional unsafe checkout",
            "uses": original["uses"],
            "with": {"fetch-depth": "0"},
        }
    else:
        extra = deepcopy(original)
        extra["name"] = "Additional checkout"
    steps.insert(0, extra)

    with pytest.raises(ci_contract.ContractError, match="exact action sequence"):
        ci_contract.validate_cloudflare_job(mutated)


def test_ci_contract_rejects_unexpected_action() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    _cloudflare_steps(mutated).append(
        {"name": "Unexpected action", "uses": "actions/cache@" + "0" * 40}
    )

    with pytest.raises(ci_contract.ContractError, match="exact action sequence"):
        ci_contract.validate_cloudflare_job(mutated)


@pytest.mark.parametrize(
    ("job_name", "validator"),
    (
        ("quality", ci_contract.validate_quality_job),
        ("cloudflare", ci_contract.validate_cloudflare_job),
        ("e2e-kind", ci_contract.validate_e2e_job),
    ),
    ids=("quality", "cloudflare", "e2e-kind"),
)
def test_ci_contract_rejects_job_shell_defaults_override(
    job_name: str, validator: object,
) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    ci_contract.object_dict(_jobs(mutated)[job_name], f"{job_name} job")["defaults"] = {
        "run": {"shell": "true {0}"}
    }

    assert callable(validator)
    with pytest.raises(ci_contract.ContractError):
        validator(mutated)


@pytest.mark.parametrize(
    ("job_name", "step_index", "validator"),
    (
        ("quality", 4, ci_contract.validate_quality_job),
        ("cloudflare", 4, ci_contract.validate_cloudflare_job),
        ("e2e-kind", 4, ci_contract.validate_e2e_job),
    ),
    ids=("quality-gate", "cloudflare-gate", "e2e-gate"),
)
def test_ci_contract_rejects_step_shell_override(
    job_name: str, step_index: int, validator: object,
) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    _job_step(mutated, job_name, step_index)["shell"] = "true {0}"

    assert callable(validator)
    with pytest.raises(ci_contract.ContractError):
        validator(mutated)


def test_ci_contract_rejects_quality_job_make_environment_override() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    quality = ci_contract.object_dict(_jobs(mutated)["quality"], "quality job")
    quality_environment = ci_contract.object_dict(quality["env"], "quality environment")
    quality_environment["MAKEFLAGS"] = "-n"

    with pytest.raises(ci_contract.ContractError):
        ci_contract.validate_quality_job(mutated)


@pytest.mark.parametrize(
    ("job_name", "step_index", "validator"),
    (
        ("quality", 4, ci_contract.validate_quality_job),
        ("cloudflare", 4, ci_contract.validate_cloudflare_job),
        ("e2e-kind", 4, ci_contract.validate_e2e_job),
    ),
    ids=("quality-gate", "cloudflare-gate", "e2e-gate"),
)
def test_ci_contract_rejects_step_make_environment_override(
    job_name: str, step_index: int, validator: object,
) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    _job_step(mutated, job_name, step_index)["env"] = {"MAKEFLAGS": "-n"}

    assert callable(validator)
    with pytest.raises(ci_contract.ContractError):
        validator(mutated)


def test_ci_contract_rejects_e2e_job_environment_override() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    ci_contract.object_dict(_jobs(mutated)["e2e-kind"], "kind E2E job")["env"] = {
        "MAKEFLAGS": "-n"
    }

    with pytest.raises(ci_contract.ContractError):
        ci_contract.validate_e2e_job(mutated)


def test_ci_contract_rejects_identity_environment_override() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    _job_step(mutated, "e2e-kind", 3)["env"] = {"EXTRA": "placeholder"}

    with pytest.raises(ci_contract.ContractError):
        ci_contract.validate_e2e_job(mutated)


@pytest.mark.parametrize(
    ("job_name", "step_index", "run", "validator"),
    (
        ("quality", 4, "printf 'make verify IMAGE_TAG=\\\"$IMAGE_TAG\\\"\\n'", ci_contract.validate_quality_job),
        ("quality", 4, "# make verify IMAGE_TAG=\\\"$IMAGE_TAG\\\"", ci_contract.validate_quality_job),
        ("e2e-kind", 4, "echo 'make e2e-kind IMAGE_TAG=\\\"$IMAGE_TAG\\\"'", ci_contract.validate_e2e_job),
        ("e2e-kind", 4, "# make e2e-kind IMAGE_TAG=\\\"$IMAGE_TAG\\\"", ci_contract.validate_e2e_job),
    ),
    ids=("quality-printf", "quality-comment", "e2e-echo", "e2e-comment"),
)
def test_ci_contract_rejects_marker_only_quality_and_e2e_gates(
    job_name: str, step_index: int, run: str, validator: object,
) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    _job_step(mutated, job_name, step_index)["run"] = run

    assert callable(validator)
    with pytest.raises(ci_contract.ContractError):
        validator(mutated)


def test_ci_contract_rejects_unexpected_cloudflare_pre_gate_step() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    steps = _cloudflare_steps(mutated)
    steps.insert(
        4,
        {
            "name": "Pre-gate PATH override",
            "run": 'printf \'make\\n\' >> "$GITHUB_PATH"',
        },
    )

    with pytest.raises(ci_contract.ContractError):
        ci_contract.validate_cloudflare_job(mutated)


@pytest.mark.parametrize("job_name", ("quality", "cloudflare", "e2e-kind"))
def test_ci_contract_rejects_step_sequence_mutations(job_name: str) -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    steps = _job_steps(mutated, job_name)
    steps.pop()
    validator = {
        "quality": ci_contract.validate_quality_job,
        "cloudflare": ci_contract.validate_cloudflare_job,
        "e2e-kind": ci_contract.validate_e2e_job,
    }[job_name]

    with pytest.raises(ci_contract.ContractError):
        validator(mutated)


def test_ci_contract_rejects_unapproved_official_action_revision() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    _cloudflare_step(mutated, "Set up pinned Node.js")["uses"] = (
        "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020"
    )

    with pytest.raises(ci_contract.ContractError, match="exact approved action revision"):
        ci_contract.validate_cloudflare_job(mutated)


def test_ci_contract_rejects_semantically_different_concurrency_group() -> None:
    workflow, _text = ci_contract.parse_workflow()
    mutated = deepcopy(workflow)
    concurrency = ci_contract.object_dict(mutated["concurrency"], "workflow concurrency")
    concurrency["group"] = "hooklane-ci-${{ github.workflow || github.ref }}"

    with pytest.raises(ci_contract.ContractError, match="canonical workflow/ref"):
        ci_contract.validate_permissions_and_concurrency(mutated)


def test_ci_contract_rejects_noop_cloudflare_make_recipes() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    mutated = makefile
    for target in ("cloudflare-test", "cloudflare-local-flow"):
        block = ci_contract.make_target_block(mutated, target)
        mutated = mutated.replace(block, f"{target}:\n\t@true", 1)

    with pytest.raises(ci_contract.ContractError, match="changed execution semantics"):
        ci_contract.validate_cloudflare_make_contract(mutated)


def test_ci_contract_rejects_fake_make_path_injection() -> None:
    sources = {
        relative_path: (ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in ci_contract.APPROVED_CONTROL_PLANE_SHA256
    }
    sources["scripts/cloudflare_ci_setup.py"] += """
fake_make = Path(os.environ["HOME"]) / "bin" / "make"
fake_make.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
with Path(os.environ["GITHUB_PATH"]).open("a", encoding="utf-8") as path_file:
    path_file.write(str(fake_make.parent) + "\\n")
"""

    with pytest.raises(ci_contract.ContractError, match="changed without approval"):
        ci_contract.validate_approved_control_plane_sources(sources)


@pytest.mark.parametrize(
    "missing",
    ("/.github/CODEOWNERS", "/scripts/**"),
    ids=("self-ownership", "control-plane-coverage"),
)
def test_ci_contract_rejects_incomplete_codeowners(missing: str) -> None:
    codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    mutated = "\n".join(
        line for line in codeowners.splitlines() if not line.startswith(f"{missing} ")
    )

    with pytest.raises(ci_contract.ContractError, match="exactly cover"):
        ci_contract.validate_codeowners_text(mutated)


def test_source_image_identity_uses_checked_out_event_sha() -> None:
    workflow, _text = ci_contract.parse_workflow()
    for job_name in ("quality", "e2e-kind"):
        identity = _job_step(workflow, job_name, 3)
        assert identity["name"] == "Determine checked-out source image identity"
        assert "env" not in identity
        assert 'SOURCE_SHA="$GITHUB_SHA"' in str(identity["run"])
        assert "pull_request.head.sha" not in str(identity["run"])


def test_personal_repository_governance_spec_is_machine_readable() -> None:
    document = (ROOT / "docs" / "CI_TRUST_MODEL.md").read_text(encoding="utf-8")
    encoded = document.split("<!-- ci-governance-spec:start -->", maxsplit=1)[1].split(
        "<!-- ci-governance-spec:end -->", maxsplit=1
    )[0]
    payload = encoded.strip().removeprefix("```json\n").removesuffix("\n```")
    specification = json.loads(payload)

    assert specification["repository"] == "sudoworks-lab/hooklane"
    assert specification["owner_type"] == "personal_account"
    assert specification["enforcement"] == "active"
    assert specification["target"] == {"type": "branch", "include": ["main"]}
    assert specification["bypass"] == [
        {"actor": "repository_administrators", "mode": "pull_requests_only"}
    ]
    assert specification["rules"] == {
        "restrict_deletions": True,
        "block_force_pushes": True,
        "require_pull_request": True,
        "required_approvals": 1,
        "require_code_owner_review": True,
        "dismiss_stale_approvals": True,
        "require_last_push_approval": False,
        "require_status_checks": True,
        "require_branch_up_to_date": True,
        "required_status_checks": [
            "Quality, security, and chart gates",
            "Cloudflare local backend gate",
            "kind delivery and recovery E2E",
        ],
    }
