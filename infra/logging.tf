resource "aws_cloudwatch_log_group" "service" {
  for_each = local.workload_names

  name              = "/ecs/${local.name}/${each.value}"
  retention_in_days = var.log_retention_days
  skip_destroy      = false

  tags = {
    Name      = "/ecs/${local.name}/${each.value}"
    Component = each.value
  }
}
