output "aws_region" {
  description = "Region containing the deployment slice."
  value       = var.aws_region
}

output "deployment_name" {
  description = "Common name prefix for the deployment slice."
  value       = local.name
}

output "alb_dns_name" {
  description = "Public ALB DNS name for the API."
  value       = local.foundation_stage_enabled ? aws_lb.api[0].dns_name : null
}

output "ecr_repository_urls" {
  description = "ECR repository URLs for API, worker, and controlled mock sink images."
  value       = { for name, repository in aws_ecr_repository.application : name => repository.repository_url }
}

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = local.foundation_stage_enabled ? aws_ecs_cluster.main[0].name : null
}

output "ecs_service_names" {
  description = "ECS service names for the vertical slice."
  value = local.foundation_stage_enabled ? {
    api       = aws_ecs_service.api[0].name
    worker    = aws_ecs_service.worker[0].name
    mock_sink = aws_ecs_service.mock_sink[0].name
  } : {}
}

output "deployment_stage" {
  description = "Selected deployment stage: artifact, foundation, or runtime."
  value       = var.deployment_stage
}

output "runtime_services_enabled" {
  description = "Whether the runtime stage configures ECS services to start tasks."
  value       = local.runtime_stage_enabled
}

output "runtime_service_desired_count" {
  description = "Effective desired count for each ECS service after the staged-deployment gate."
  value       = local.runtime_service_desired_count
}

output "redis_primary_endpoint_address" {
  description = "Private ElastiCache primary endpoint address."
  value       = local.foundation_stage_enabled ? aws_elasticache_replication_group.redis[0].primary_endpoint_address : null
}

output "redis_secret_arn" {
  description = "Secrets Manager ARN containing the generated Redis URL. The secret value is never output."
  value       = local.foundation_stage_enabled ? aws_secretsmanager_secret.redis_url[0].arn : null
}

output "security_group_ids" {
  description = "Security group IDs for the public ALB and private workloads."
  value = local.foundation_stage_enabled ? {
    alb       = aws_security_group.alb[0].id
    api       = aws_security_group.api[0].id
    worker    = aws_security_group.worker[0].id
    mock_sink = aws_security_group.mock_sink[0].id
    redis     = aws_security_group.redis[0].id
  } : {}
}
