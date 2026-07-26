resource "aws_secretsmanager_secret" "redis_url" {
  count = local.foundation_stage_enabled ? 1 : 0

  name                    = "${local.name}/redis-url"
  recovery_window_in_days = var.secret_recovery_window_days

  tags = {
    Name = "${local.name}-redis-url"
  }
}

# The value is assembled from the managed endpoint and the optional sensitive
# auth token. It is never exposed as a Terraform output.
resource "aws_secretsmanager_secret_version" "redis_url" {
  count = local.foundation_stage_enabled ? 1 : 0

  secret_id     = aws_secretsmanager_secret.redis_url[0].id
  secret_string = local.redis_url
}
