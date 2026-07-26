from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"


def test_bootstrap_uses_sse_s3_without_bucket_key() -> None:
    source = (INFRA / "bootstrap" / "main.tf").read_text(encoding="utf-8")

    assert 'sse_algorithm = "AES256"' in source
    assert "bucket_key_enabled" not in source
    assert "force_destroy = var.force_destroy" in source


def test_runtime_services_are_disabled_until_images_are_available() -> None:
    variables = (INFRA / "variables.tf").read_text(encoding="utf-8")
    locals_source = (INFRA / "locals.tf").read_text(encoding="utf-8")
    ecs_source = (INFRA / "ecs.tf").read_text(encoding="utf-8")
    example = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")

    runtime_variable = variables.split('variable "runtime_services_enabled" {', maxsplit=1)[1]
    runtime_variable = runtime_variable.split("\n}", maxsplit=1)[0]

    assert "default     = false" in runtime_variable
    assert (
        "runtime_service_desired_count = var.runtime_services_enabled ? "
        "var.desired_count : 0"
    ) in locals_source
    assert ecs_source.count("local.runtime_service_desired_count") == 3
    assert "runtime_services_enabled = false" in example
