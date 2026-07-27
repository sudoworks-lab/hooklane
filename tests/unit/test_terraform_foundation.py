from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"


def test_bootstrap_uses_sse_s3_without_bucket_key() -> None:
    source = (INFRA / "bootstrap" / "main.tf").read_text(encoding="utf-8")

    assert 'sse_algorithm = "AES256"' in source
    assert "bucket_key_enabled" not in source
    assert "force_destroy = var.force_destroy" in source

    variables = (INFRA / "bootstrap" / "variables.tf").read_text(encoding="utf-8")
    example = (INFRA / "bootstrap" / "terraform.tfvars.example").read_text(
        encoding="utf-8"
    )
    bucket_variable = variables.split('variable "bucket_name" {', maxsplit=1)[1]
    bucket_variable = bucket_variable.split("\n}", maxsplit=1)[0]
    assert "default" not in bucket_variable
    assert 'bucket_name = "hooklane-dev-terraform-state-plan-only"' in example


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


def test_mock_sink_a_record_service_registry_omits_container_port() -> None:
    ecs_source = (INFRA / "ecs.tf").read_text(encoding="utf-8")

    cloud_map_service = ecs_source.split(
        'resource "aws_service_discovery_service" "mock_sink" {', maxsplit=1
    )[1].split('\n}\n\nresource "aws_ecs_task_definition"', maxsplit=1)[0]
    mock_sink_service = ecs_source.split(
        'resource "aws_ecs_service" "mock_sink" {', maxsplit=1
    )[1].split("\n}\n", maxsplit=1)[0]

    assert 'type = "A"' in cloud_map_service
    assert "registry_arn = aws_service_discovery_service.mock_sink[0].arn" in mock_sink_service
    assert "container_name" not in mock_sink_service
    assert "container_port" not in mock_sink_service


def test_worker_liveness_contract_uses_the_local_metrics_surface() -> None:
    ecs_source = (INFRA / "ecs.tf").read_text(encoding="utf-8")
    compose_source = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    worker_chart = (
        ROOT / "charts" / "hooklane" / "templates" / "worker-deployment.yaml"
    ).read_text(encoding="utf-8")
    worker_task = ecs_source.split(
        'resource "aws_ecs_task_definition" "worker" {', maxsplit=1
    )[1].split('resource "aws_ecs_task_definition" "mock_sink" {', maxsplit=1)[0]
    worker_compose = compose_source.split("\n  worker:\n", maxsplit=1)[1]

    local_probe = (
        "python -c \\\"import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:9090/metrics', timeout=2).close()\\\""
    )
    assert "containerPort = 9090" in worker_task
    assert f'command     = ["CMD-SHELL", "{local_probe}"]' in worker_task
    assert "hooklane.worker.health startup" not in worker_task
    assert "HOOKLANE_REDIS_URL" not in worker_task.split("healthCheck = {", maxsplit=1)[1].split(
        "linuxParameters", maxsplit=1
    )[0]
    for setting in ("interval    = 30", "timeout     = 5", "retries     = 3", "startPeriod = 30"):
        assert setting in worker_task

    assert "127.0.0.1:9090/metrics" in worker_compose
    assert "xinfo_groups" not in worker_compose
    assert "tcpSocket:" in worker_chart
    assert "port: metrics" in worker_chart


def test_alb_egress_is_limited_to_the_api_target_port() -> None:
    security_source = (INFRA / "security.tf").read_text(encoding="utf-8")
    alb_security_group = security_source.split(
        'resource "aws_security_group" "alb" {', maxsplit=1
    )[1].split('\n}\n\nresource "aws_security_group_rule" "alb_https"', maxsplit=1)[0]

    assert 'from_port       = 8080' in alb_security_group
    assert 'to_port         = 8080' in alb_security_group
    assert 'security_groups = [aws_security_group.api[0].id]' in alb_security_group
    assert 'cidr_blocks      = ["0.0.0.0/0"]' not in alb_security_group


def test_runtime_apply_guards_reject_ambiguous_image_and_ingress() -> None:
    variables = (INFRA / "variables.tf").read_text(encoding="utf-8")
    locals_source = (INFRA / "locals.tf").read_text(encoding="utf-8")
    security_source = (INFRA / "security.tf").read_text(encoding="utf-8")
    ecs_source = (INFRA / "ecs.tf").read_text(encoding="utf-8")
    example = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")

    assert "^git-[0-9a-f]{40}$" in locals_source
    assert "runtime_image_tag_valid" in ecs_source
    assert 'var.deployment_stage != "runtime"' in ecs_source
    assert '"192.0.2.1/32", "0.0.0.0/0", "::/0"' in security_source
    assert 'var.deployment_stage != "runtime"' in security_source
    assert 'var.image_tag != "git-0000000000000000000000000000000000000000"' in locals_source
    assert 'image_tag              = "git-REPLACE_WITH_40_HEX_COMMIT"' in example
    assert 'length(var.alb_ingress_cidr_blocks) == 1' in variables
    assert '^[0-9]{1,3}(\\\\.[0-9]{1,3}){3}/32$' in variables
    assert 'can(cidrhost(var.alb_ingress_cidr_blocks[0], 0))' in variables
    assert 'default     = "0.1.1"' in variables
