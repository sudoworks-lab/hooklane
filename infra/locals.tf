locals {
  name = "${var.project_name}-${var.environment}"

  foundation_stage_enabled = contains(["foundation", "runtime"], var.deployment_stage)
  runtime_stage_enabled    = var.deployment_stage == "runtime"
  runtime_image_tag_valid  = can(regex("^git-[0-9a-f]{40}$", var.image_tag)) && var.image_tag != "git-0000000000000000000000000000000000000000"

  azs = local.foundation_stage_enabled ? slice(data.aws_availability_zones.available[0].names, 0, var.availability_zone_count) : []

  az_to_index = {
    for index, az in local.azs : az => index
  }

  public_subnet_cidrs = {
    for index, az in local.azs : az => cidrsubnet(var.vpc_cidr, 4, index)
  }

  private_subnet_cidrs = {
    for index, az in local.azs : az => cidrsubnet(var.vpc_cidr, 4, index + 8)
  }

  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Component   = "hooklane"
    },
    var.tags,
  )

  workload_names = toset(["api", "worker", "mock-sink"])

  runtime_service_desired_count = local.runtime_stage_enabled ? var.desired_count : 0

  mock_sink_url = local.foundation_stage_enabled ? "http://${aws_service_discovery_service.mock_sink[0].name}.${aws_service_discovery_private_dns_namespace.internal[0].name}:8080/internal/deliveries" : null

  downstream_url = local.foundation_stage_enabled ? (var.controlled_downstream_url == null ? local.mock_sink_url : var.controlled_downstream_url) : null

  redis_url = local.foundation_stage_enabled ? sensitive(
    var.redis_auth_token == null
    ? "rediss://${aws_elasticache_replication_group.redis[0].primary_endpoint_address}:6379/0"
    : "rediss://:${urlencode(var.redis_auth_token)}@${aws_elasticache_replication_group.redis[0].primary_endpoint_address}:6379/0"
  ) : null
}
