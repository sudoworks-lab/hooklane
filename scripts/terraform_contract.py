"""Validate the Terraform foundation without AWS credentials or resource access."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
BOOTSTRAP = INFRA / "bootstrap"
REQUIRED_FILES = (
    "versions.tf",
    "variables.tf",
    "locals.tf",
    "network.tf",
    "security.tf",
    "ecr.tf",
    "cache.tf",
    "secret_store.tf",
    "iam.tf",
    "ecs.tf",
    "alb.tf",
    "logging.tf",
    "outputs.tf",
    "README.md",
    "DESTROY.md",
)
BOOTSTRAP_REQUIRED_FILES = (
    "versions.tf",
    "variables.tf",
    "main.tf",
    "outputs.tf",
    "README.md",
)
REQUIRED_RESOURCES = (
    'resource "aws_vpc"',
    'resource "aws_subnet" "public"',
    'resource "aws_subnet" "private"',
    'resource "aws_security_group" "alb"',
    'resource "aws_security_group" "api"',
    'resource "aws_security_group" "worker"',
    'resource "aws_security_group" "redis"',
    'resource "aws_ecr_repository"',
    'resource "aws_ecs_cluster"',
    'resource "aws_ecs_task_definition" "api"',
    'resource "aws_ecs_task_definition" "worker"',
    'resource "aws_ecs_task_definition" "mock_sink"',
    'resource "aws_ecs_service" "api"',
    'resource "aws_ecs_service" "worker"',
    'resource "aws_ecs_service" "mock_sink"',
    'resource "aws_lb"',
    'resource "aws_lb_target_group"',
    'resource "aws_elasticache_replication_group"',
    'resource "aws_cloudwatch_log_group"',
    'resource "aws_secretsmanager_secret"',
    'resource "aws_iam_role" "ecs_execution"',
    'resource "aws_iam_role" "ecs_task"',
)
FORBIDDEN_MARKERS = (
    "aws_instance",
    "assign_public_ip = true",
    'Action = ["*"]',
    'actions = ["*"]',
)


def fail(message: str) -> None:
    raise RuntimeError(message)


def read_terraform_source() -> str:
    files = tuple(sorted(INFRA.glob("*.tf")))
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def validate_static_contract() -> None:
    if not INFRA.is_dir():
        fail("infra directory is missing")
    for filename in REQUIRED_FILES:
        if not (INFRA / filename).is_file():
            fail(f"required Terraform foundation file is missing: {filename}")
    if not BOOTSTRAP.is_dir():
        fail("Terraform bootstrap directory is missing")
    for filename in BOOTSTRAP_REQUIRED_FILES:
        if not (BOOTSTRAP / filename).is_file():
            fail(f"required Terraform bootstrap file is missing: {filename}")

    source = read_terraform_source()
    for marker in REQUIRED_RESOURCES:
        if marker not in source:
            fail(f"Terraform foundation is missing resource contract: {marker}")
    for marker in FORBIDDEN_MARKERS:
        if marker in source:
            fail(f"Terraform foundation contains prohibited contract: {marker}")

    required_fragments = (
        'required_version = ">= 1.15.0, < 2.0.0"',
        'version = "= 5.95.0"',
        'backend "s3" {}',
        'default     = "artifact"',
        'contains(["artifact", "foundation", "runtime"], var.deployment_stage)',
        'foundation_stage_enabled = contains(["foundation", "runtime"], var.deployment_stage)',
        'runtime_stage_enabled    = var.deployment_stage == "runtime"',
        'runtime_image_tag_valid  = can(regex("^git-[0-9a-f]{40}$", var.image_tag)) && var.image_tag != "git-0000000000000000000000000000000000000000"',
        'default     = 1',
        "valueFrom = aws_secretsmanager_secret.redis_url[0].arn",
        "secret_string = local.redis_url",
        "downstream_url = local.foundation_stage_enabled ? (var.controlled_downstream_url == null ? local.mock_sink_url : var.controlled_downstream_url) : null",
        'default     = ["192.0.2.1/32"]',
        "assign_public_ip = false",
        "deployment_circuit_breaker",
        'rollback = true',
        "use_lockfile = true",
        'variable "deployment_stage"',
        "runtime_service_desired_count = local.runtime_stage_enabled ? var.desired_count : 0",
        "desired_count",
        'var.deployment_stage != "runtime" || local.runtime_image_tag_valid',
        '"192.0.2.1/32", "0.0.0.0/0", "::/0"',
        'length(var.alb_ingress_cidr_blocks) == 1',
        'can(regex("^[0-9]{1,3}(\\\\.[0-9]{1,3}){3}/32$", var.alb_ingress_cidr_blocks[0]))',
        'can(cidrhost(var.alb_ingress_cidr_blocks[0], 0))',
    )
    for fragment in required_fragments:
        if fragment not in source and fragment not in (INFRA / "backend.hcl.example").read_text(
            encoding="utf-8"
        ):
            fail(f"Terraform foundation is missing required safety fragment: {fragment}")

    outputs = (INFRA / "outputs.tf").read_text(encoding="utf-8")
    if "secret_string" in outputs or 'output "redis_url"' in outputs:
        fail("Terraform outputs must not expose a secret value")

    for path in (INFRA / "terraform.tfvars.example", INFRA / "backend.hcl.example"):
        text = path.read_text(encoding="utf-8")
        if "password=" in text or "secret=" in text or "token=" in text:
            fail(f"example configuration contains a credential-bearing query: {path.name}")

    bootstrap_source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(BOOTSTRAP.glob("*.tf"))
    )
    for marker in (
        'resource "aws_s3_bucket" "state"',
        'resource "aws_s3_bucket_versioning" "state"',
        'resource "aws_s3_bucket_server_side_encryption_configuration" "state"',
        'resource "aws_s3_bucket_public_access_block" "state"',
        'resource "aws_s3_bucket_ownership_controls" "state"',
        'resource "aws_s3_bucket_policy" "require_tls"',
        'force_destroy = var.force_destroy',
        'sse_algorithm = "AES256"',
        'required_version = ">= 1.15.0, < 2.0.0"',
    ):
        if marker not in bootstrap_source:
            fail(f"Terraform bootstrap is missing required contract: {marker}")
    if "bucket_key_enabled" in bootstrap_source:
        fail("Terraform bootstrap must not configure S3 Bucket Key with SSE-S3")
    bootstrap_variables = (BOOTSTRAP / "variables.tf").read_text(encoding="utf-8")
    bucket_variable = bootstrap_variables.split('variable "bucket_name" {', maxsplit=1)[1]
    bucket_variable = bucket_variable.split("\n}", maxsplit=1)[0]
    if "default" in bucket_variable:
        fail("Terraform bootstrap bucket_name must be explicitly supplied before apply")

    ecs_source = (INFRA / "ecs.tf").read_text(encoding="utf-8")
    if ecs_source.count("local.runtime_service_desired_count") != 3:
        fail("all three ECS services must use the staged runtime desired count")
    worker_task = ecs_source.split(
        'resource "aws_ecs_task_definition" "worker" {', maxsplit=1
    )[1].split('resource "aws_ecs_task_definition" "mock_sink" {', maxsplit=1)[0]
    worker_health_check = worker_task.split("healthCheck = {", maxsplit=1)[1].split(
        "linuxParameters", maxsplit=1
    )[0]
    worker_liveness_command = (
        "python -c \\\"import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:9090/metrics', timeout=2).close()\\\""
    )
    if f'command     = ["CMD-SHELL", "{worker_liveness_command}"]' not in worker_health_check:
        fail("worker ECS health check must probe only the local metrics surface")
    if "hooklane.worker.health startup" in worker_health_check:
        fail("worker ECS health check must not make Redis readiness a liveness condition")
    if "HOOKLANE_REDIS_URL" in worker_health_check:
        fail("worker ECS health check must not expose the Redis connection setting")

    example_variables = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
    if 'deployment_stage       = "artifact"' not in example_variables:
        fail("Terraform example must start in the ECR-only artifact stage")
    if 'image_tag              = "git-REPLACE_WITH_40_HEX_COMMIT"' not in example_variables:
        fail("Terraform example must require replacement of the runtime image placeholder")

    for filename in (
        "network.tf",
        "security.tf",
        "cache.tf",
        "secret_store.tf",
        "iam.tf",
        "ecs.tf",
        "alb.tf",
        "logging.tf",
    ):
        if "local.foundation_stage_enabled" not in (INFRA / filename).read_text(encoding="utf-8"):
            fail(f"Terraform foundation file is not gated from the artifact stage: {filename}")


def run_terraform_checks() -> None:
    terraform = shutil.which("terraform")
    required = os.environ.get("HOOKLANE_TERRAFORM_REQUIRED") == "1" or os.environ.get("CI") == "true"
    if terraform is None:
        if required:
            fail("Terraform CLI is required for CI verification but is unavailable")
        print("[degraded] terraform CLI is unavailable; static Terraform contract passed")
        return

    fmt_result = subprocess.run(
        [terraform, "fmt", "-check", "-recursive"],
        cwd=INFRA,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if fmt_result.returncode != 0:
        fail("terraform fmt failed; diagnostics were intentionally suppressed")
    print("[ok] terraform fmt")
    for module in (INFRA, BOOTSTRAP):
        label = module.relative_to(ROOT).as_posix()
        with tempfile.TemporaryDirectory(prefix="hooklane-tf-contract-") as runtime_root:
            runtime_path = Path(runtime_root)
            temporary_home = runtime_path / "home"
            temporary_home.mkdir()
            environment = {
                "HOME": str(temporary_home),
                "PATH": os.environ.get("PATH", ""),
                "TF_DATA_DIR": str(runtime_path / "terraform-data"),
                "TF_IN_AUTOMATION": "1",
            }
            init_result = subprocess.run(
                [terraform, "init", "-backend=false", "-input=false", "-upgrade=false"],
                cwd=module,
                env=environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if init_result.returncode != 0:
                fail(f"terraform init failed in {label}; diagnostics were intentionally suppressed")
            validate_result = subprocess.run(
                [terraform, "validate"],
                cwd=module,
                env=environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if validate_result.returncode != 0:
                fail(f"terraform validate failed in {label}; diagnostics were intentionally suppressed")
        print(f"[ok] terraform init/validate: {label}")


def main() -> int:
    try:
        validate_static_contract()
        run_terraform_checks()
    except (OSError, RuntimeError) as error:
        print(f"[fail] Terraform contract: {error}", file=sys.stderr)
        return 1
    print("[ok] Terraform resource, security boundary, secret output, cost default, and rollback contracts passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
