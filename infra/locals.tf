locals {
  name = "${var.project_name}-${var.environment}"

  azs = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)

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

  runtime_service_desired_count = var.runtime_services_enabled ? var.desired_count : 0

  mock_sink_url = "http://${aws_service_discovery_service.mock_sink.name}.${aws_service_discovery_private_dns_namespace.internal.name}:8080/internal/deliveries"

  downstream_url = var.controlled_downstream_url == null ? local.mock_sink_url : var.controlled_downstream_url

  redis_url = sensitive(
    var.redis_auth_token == null
    ? "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
    : "rediss://:${urlencode(var.redis_auth_token)}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
  )
}
