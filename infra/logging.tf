resource "aws_cloudwatch_log_group" "service" {
  for_each = local.foundation_stage_enabled ? local.workload_names : toset([])

  name              = "/ecs/${local.name}/${each.value}"
  retention_in_days = var.log_retention_days
  skip_destroy      = false

  tags = {
    Name      = "/ecs/${local.name}/${each.value}"
    Component = each.value
  }
}
