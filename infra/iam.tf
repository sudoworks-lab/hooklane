data "aws_iam_policy_document" "ecs_task_assume_role" {
  count = local.foundation_stage_enabled ? 1 : 0

  statement {
    effect = "Allow"

    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  count = local.foundation_stage_enabled ? 1 : 0

  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume_role[0].json

  tags = {
    Name = "${local.name}-ecs-execution"
  }
}

resource "aws_iam_role" "ecs_task" {
  count = local.foundation_stage_enabled ? 1 : 0

  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume_role[0].json

  tags = {
    Name = "${local.name}-ecs-task"
  }
}

data "aws_iam_policy_document" "ecs_execution" {
  count = local.foundation_stage_enabled ? 1 : 0

  statement {
    sid       = "EcrAuthorization"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPullApplicationImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for repository in aws_ecr_repository.application : repository.arn]
  }

  statement {
    sid    = "WriteApplicationLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [for log_group in aws_cloudwatch_log_group.service : "${log_group.arn}:*"]
  }

  statement {
    sid       = "ReadRedisConnectionSecret"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.redis_url[0].arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution" {
  count = local.foundation_stage_enabled ? 1 : 0

  name   = "${local.name}-ecs-execution"
  role   = aws_iam_role.ecs_execution[0].id
  policy = data.aws_iam_policy_document.ecs_execution[0].json
}
