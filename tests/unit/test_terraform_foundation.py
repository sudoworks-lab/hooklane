from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"


def test_bootstrap_uses_sse_s3_without_bucket_key() -> None:
    source = (INFRA / "bootstrap" / "main.tf").read_text(encoding="utf-8")

    assert 'sse_algorithm = "AES256"' in source
    assert "bucket_key_enabled" not in source
    assert "force_destroy = var.force_destroy" in source


def test_artifact_stage_keeps_non_ecr_resources_out_of_the_first_apply() -> None:
    variables = (INFRA / "variables.tf").read_text(encoding="utf-8")
    locals_source = (INFRA / "locals.tf").read_text(encoding="utf-8")
    ecs_source = (INFRA / "ecs.tf").read_text(encoding="utf-8")
    example = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
    ecr_source = (INFRA / "ecr.tf").read_text(encoding="utf-8")

    stage_variable = variables.split('variable "deployment_stage" {', maxsplit=1)[1]
    stage_variable = stage_variable.split("\n}", maxsplit=1)[0]

    assert 'default     = "artifact"' in stage_variable
    assert 'contains(["artifact", "foundation", "runtime"], var.deployment_stage)' in stage_variable
    assert (
        'foundation_stage_enabled = contains(["foundation", "runtime"], '
        "var.deployment_stage)"
    ) in locals_source
    assert 'runtime_stage_enabled    = var.deployment_stage == "runtime"' in locals_source
    assert (
        "runtime_service_desired_count = local.runtime_stage_enabled ? "
        "var.desired_count : 0"
    ) in locals_source
    assert ecs_source.count("local.runtime_service_desired_count") == 3
    assert 'deployment_stage       = "artifact"' in example
    assert ecr_source.count('resource "aws_ecr_repository"') == 1
    assert ecr_source.count('resource "aws_ecr_lifecycle_policy"') == 1

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
        assert "local.foundation_stage_enabled" in (INFRA / filename).read_text(
            encoding="utf-8"
        )
