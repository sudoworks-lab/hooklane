"""Validate the baseline GitHub Actions workflow without contacting GitHub."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACTION_REFERENCE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


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


def parse_workflow() -> tuple[dict[str, object], str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    try:
        document: object = yaml.load(text, Loader=yaml.BaseLoader)
    except yaml.YAMLError as error:
        raise ContractError("workflow YAML could not be parsed") from error
    return object_dict(document, "workflow"), text


def validate_events(workflow: dict[str, object]) -> None:
    events = object_dict(workflow.get("on"), "workflow triggers")
    if set(events) != {"pull_request", "push"}:
        raise ContractError("workflow must target pull_request and push only")
    push = object_dict(events.get("push"), "push trigger")
    branches = object_list(push.get("branches"), "push branches")
    if branches != ["main"]:
        raise ContractError("push trigger must target main only")


def validate_permissions_and_concurrency(workflow: dict[str, object]) -> None:
    permissions = object_dict(workflow.get("permissions"), "workflow permissions")
    if permissions != {"contents": "read"}:
        raise ContractError("workflow permissions must be contents: read only")
    concurrency = object_dict(workflow.get("concurrency"), "workflow concurrency")
    group = concurrency.get("group")
    if not isinstance(group, str) or "github.workflow" not in group or "github.ref" not in group:
        raise ContractError("concurrency must be scoped to workflow and ref")
    if concurrency.get("cancel-in-progress") != "true":
        raise ContractError("concurrency must cancel superseded runs")
    defaults = object_dict(workflow.get("defaults"), "workflow defaults")
    run_defaults = object_dict(defaults.get("run"), "run defaults")
    if run_defaults.get("shell") != "bash":
        raise ContractError("workflow shell must be bash")


def validate_quality_job(workflow: dict[str, object]) -> None:
    jobs = object_dict(workflow.get("jobs"), "workflow jobs")
    if set(jobs) != {"quality", "e2e-kind"}:
        raise ContractError("workflow must contain quality and kind E2E jobs")
    quality = object_dict(jobs.get("quality"), "quality job")
    if quality.get("runs-on") != "ubuntu-24.04":
        raise ContractError("quality job must use the fixed Ubuntu runner label")
    timeout = quality.get("timeout-minutes")
    if not isinstance(timeout, str) or not timeout.isdigit() or int(timeout) > 40:
        raise ContractError("quality job requires a bounded timeout")
    if "permissions" in quality:
        raise ContractError("quality job must not expand top-level permissions")
    quality_environment = object_dict(quality.get("env"), "quality environment")
    if quality_environment.get("HOOKLANE_TERRAFORM_REQUIRED") != "1":
        raise ContractError("quality job must require Terraform instead of allowing a skip")

    steps = object_list(quality.get("steps"), "quality steps")
    action_references: list[str] = []
    run_commands: list[str] = []
    for raw_step in steps:
        step = object_dict(raw_step, "quality step")
        name = step.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ContractError("every workflow step needs a diagnostic name")
        uses = step.get("uses")
        if isinstance(uses, str):
            action_references.append(uses)
            if not ACTION_REFERENCE.fullmatch(uses):
                raise ContractError(f"action reference is not a full commit SHA: {uses}")
        run = step.get("run")
        if isinstance(run, str):
            run_commands.append(run)
            if "${{" in run:
                raise ContractError("run commands must not interpolate untrusted contexts")

    if len(action_references) != 2:
        raise ContractError("baseline workflow must use checkout and setup-python only")
    combined_commands = "\n".join(run_commands)
    for required in ("make ci-setup", "terraform version", "make verify"):
        if required not in combined_commands:
            raise ContractError(f"workflow does not call local target: {required}")


def validate_e2e_job(workflow: dict[str, object]) -> None:
    jobs = object_dict(workflow.get("jobs"), "workflow jobs")
    e2e = object_dict(jobs.get("e2e-kind"), "kind E2E job")
    if e2e.get("needs") != ["quality"]:
        raise ContractError("kind E2E must run only after the quality job")
    if e2e.get("runs-on") != "ubuntu-24.04":
        raise ContractError("kind E2E must use the fixed Ubuntu runner label")
    timeout = e2e.get("timeout-minutes")
    if not isinstance(timeout, str) or not timeout.isdigit() or int(timeout) > 30:
        raise ContractError("kind E2E requires a bounded timeout")
    if "permissions" in e2e:
        raise ContractError("kind E2E must not expand top-level permissions")

    steps = object_list(e2e.get("steps"), "kind E2E steps")
    action_references: list[str] = []
    run_commands: list[str] = []
    artifact_step: dict[str, object] | None = None
    cleanup_step: dict[str, object] | None = None
    for raw_step in steps:
        step = object_dict(raw_step, "kind E2E step")
        name = step.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ContractError("every kind E2E step needs a diagnostic name")
        uses = step.get("uses")
        if isinstance(uses, str):
            action_references.append(uses)
            if not ACTION_REFERENCE.fullmatch(uses):
                raise ContractError(f"action reference is not a full commit SHA: {uses}")
            if uses.startswith("actions/upload-artifact@"):
                artifact_step = step
        run = step.get("run")
        if isinstance(run, str):
            run_commands.append(run)
            if "${{" in run:
                raise ContractError("kind E2E commands must not interpolate untrusted contexts")
            if "make cluster-down" in run:
                cleanup_step = step

    if len(action_references) != 3:
        raise ContractError("kind E2E must use checkout, setup-python, and upload-artifact")
    combined_commands = "\n".join(run_commands)
    for required in ("make ci-setup", "make e2e-kind", "make cluster-down"):
        if required not in combined_commands:
            raise ContractError(f"kind E2E job does not call local target: {required}")
    if artifact_step is None or artifact_step.get("if") != "failure()":
        raise ContractError("diagnostics must upload only after failure")
    artifact_options = object_dict(artifact_step.get("with"), "artifact options")
    if artifact_options.get("path") != "artifacts/kind-e2e-*":
        raise ContractError("diagnostics artifact path does not match the local E2E")
    if cleanup_step is None or cleanup_step.get("if") != "always()":
        raise ContractError("kind cluster cleanup must run with always()")


def validate_prohibited_content(text: str) -> None:
    prohibited = (
        "pull_request_target",
        "${{ secrets.",
        "git push",
        "docker push",
        "helm install",
        ":latest",
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
        validate_quality_job(workflow)
        validate_e2e_job(workflow)
        validate_prohibited_content(text)
    except (OSError, ContractError) as error:
        print(f"[fail] CI contract: {error}")
        return 1
    print(
        "[ok] CI YAML, triggers, permissions, concurrency, action SHA pins, local Make "
        "targets, and untrusted-input contract passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
