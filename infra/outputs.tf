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
  value       = aws_lb.api.dns_name
}

output "ecr_repository_urls" {
  description = "ECR repository URLs for API, worker, and controlled mock sink images."
  value       = { for name, repository in aws_ecr_repository.application : name => repository.repository_url }
}

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_names" {
  description = "ECS service names for the vertical slice."
  value = {
    api       = aws_ecs_service.api.name
    worker    = aws_ecs_service.worker.name
    mock_sink = aws_ecs_service.mock_sink.name
  }
}

output "runtime_services_enabled" {
  description = "Whether the ECS services are configured to start tasks."
  value       = var.runtime_services_enabled
}

output "runtime_service_desired_count" {
  description = "Effective desired count for each ECS service after the staged-deployment gate."
  value       = local.runtime_service_desired_count
}

output "redis_primary_endpoint_address" {
  description = "Private ElastiCache primary endpoint address."
  value       = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "redis_secret_arn" {
  description = "Secrets Manager ARN containing the generated Redis URL. The secret value is never output."
  value       = aws_secretsmanager_secret.redis_url.arn
}

output "security_group_ids" {
  description = "Security group IDs for the public ALB and private workloads."
  value = {
    alb       = aws_security_group.alb.id
    api       = aws_security_group.api.id
    worker    = aws_security_group.worker.id
    mock_sink = aws_security_group.mock_sink.id
    redis     = aws_security_group.redis.id
  }
}
