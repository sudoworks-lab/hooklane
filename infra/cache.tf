resource "aws_elasticache_subnet_group" "redis" {
  count = local.foundation_stage_enabled ? 1 : 0

  name       = "${local.name}-redis"
  subnet_ids = [for subnet in aws_subnet.private : subnet.id]

  tags = {
    Name = "${local.name}-redis"
  }
}

resource "aws_elasticache_replication_group" "redis" {
  count = local.foundation_stage_enabled ? 1 : 0

  replication_group_id = substr(replace(local.name, "_", "-"), 0, 20)
  description          = "Hooklane Redis-compatible queue and status store"

  lifecycle {
    precondition {
      condition     = !var.enable_cache_multi_az || var.cache_num_nodes > 1
      error_message = "enable_cache_multi_az requires cache_num_nodes greater than one."
    }
  }

  engine               = var.cache_engine
  engine_version       = var.cache_engine_version
  node_type            = var.cache_node_type
  num_cache_clusters   = var.cache_num_nodes
  port                 = 6379
  parameter_group_name = var.cache_engine == "valkey" ? "default.valkey7" : "default.redis7"

  automatic_failover_enabled = var.cache_num_nodes > 1
  multi_az_enabled           = var.enable_cache_multi_az
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  auth_token                 = var.redis_auth_token
  apply_immediately          = var.cache_apply_immediately

  subnet_group_name  = aws_elasticache_subnet_group.redis[0].name
  security_group_ids = [aws_security_group.redis[0].id]

  tags = {
    Name = "${local.name}-redis"
  }
}
