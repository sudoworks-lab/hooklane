"""Validate the Terraform foundation without AWS credentials or resource access."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys


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
        'version = "= 5.95.0"',
        'backend "s3" {}',
        'default     = false',
        'default     = 1',
        "valueFrom = aws_secretsmanager_secret.redis_url.arn",
        "secret_string = local.redis_url",
        "downstream_url = var.controlled_downstream_url == null ? local.mock_sink_url : var.controlled_downstream_url",
        'default     = ["192.0.2.1/32"]',
        "assign_public_ip = false",
        "deployment_circuit_breaker",
        'rollback = true',
        "use_lockfile = true",
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
    ):
        if marker not in bootstrap_source:
            fail(f"Terraform bootstrap is missing required contract: {marker}")


def run_terraform_checks() -> None:
    terraform = shutil.which("terraform")
    if terraform is None:
        print("[skip] terraform CLI is unavailable; static Terraform contract passed")
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
        init_result = subprocess.run(
            [terraform, "init", "-backend=false", "-input=false", "-upgrade=false"],
            cwd=module,
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
