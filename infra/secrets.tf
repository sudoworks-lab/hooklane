resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "${local.name}/redis-url"
  recovery_window_in_days = var.secret_recovery_window_days

  tags = {
    Name = "${local.name}-redis-url"
  }
}

# The value is assembled from the managed endpoint and the optional sensitive
# auth token. It is never exposed as a Terraform output.
resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id     = aws_secretsmanager_secret.redis_url.id
  secret_string = local.redis_url
}
