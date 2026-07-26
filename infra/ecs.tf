resource "aws_ecs_cluster" "main" {
  count = local.foundation_stage_enabled ? 1 : 0

  name = local.name

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }

  tags = {
    Name = local.name
  }
}

resource "aws_service_discovery_private_dns_namespace" "internal" {
  count = local.foundation_stage_enabled ? 1 : 0

  name        = "${local.name}.internal"
  description = "Private Cloud Map namespace for Hooklane service discovery"
  vpc         = aws_vpc.main[0].id

  tags = {
    Name = "${local.name}.internal"
  }
}

resource "aws_service_discovery_service" "mock_sink" {
  count = local.foundation_stage_enabled ? 1 : 0

  name = "mock-sink"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal[0].id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  tags = {
    Name = "${local.name}-mock-sink"
  }
}

resource "aws_ecs_task_definition" "api" {
  count = local.foundation_stage_enabled ? 1 : 0

  family                   = "${local.name}-api"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  task_role_arn            = aws_iam_role.ecs_task[0].arn

  container_definitions = jsonencode([
    {
      name                   = "api"
      image                  = "${aws_ecr_repository.application["api"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      portMappings = [{
        name          = "http"
        containerPort = 8080
        protocol      = "tcp"
      }]
      secrets = [{
        name      = "HOOKLANE_REDIS_URL"
        valueFrom = aws_secretsmanager_secret.redis_url[0].arn
      }]
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2).close()\""]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      linuxParameters = {
        tmpfs = [{
          containerPath = "/tmp"
          size          = 16
          mountOptions  = ["noexec", "nosuid"]
        }]
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.service["api"].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])

  tags = {
    Name = "${local.name}-api"
  }
}

resource "aws_ecs_task_definition" "worker" {
  count = local.foundation_stage_enabled ? 1 : 0

  family                   = "${local.name}-worker"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  task_role_arn            = aws_iam_role.ecs_task[0].arn

  container_definitions = jsonencode([
    {
      name                   = "worker"
      image                  = "${aws_ecr_repository.application["worker"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      portMappings = [{
        name          = "metrics"
        containerPort = 9090
        protocol      = "tcp"
      }]
      environment = [{
        name  = "HOOKLANE_DOWNSTREAM_URL"
        value = local.downstream_url
      }]
      secrets = [{
        name      = "HOOKLANE_REDIS_URL"
        valueFrom = aws_secretsmanager_secret.redis_url[0].arn
      }]
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:9090/metrics', timeout=2).close()\""]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      linuxParameters = {
        tmpfs = [{
          containerPath = "/tmp"
          size          = 16
          mountOptions  = ["noexec", "nosuid"]
        }]
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.service["worker"].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])

  tags = {
    Name = "${local.name}-worker"
  }
}

resource "aws_ecs_task_definition" "mock_sink" {
  count = local.foundation_stage_enabled ? 1 : 0

  family                   = "${local.name}-mock-sink"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution[0].arn
  task_role_arn            = aws_iam_role.ecs_task[0].arn

  container_definitions = jsonencode([
    {
      name                   = "mock-sink"
      image                  = "${aws_ecr_repository.application["mock-sink"].repository_url}:${var.image_tag}"
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      portMappings = [{
        name          = "http"
        containerPort = 8080
        protocol      = "tcp"
      }]
      environment = [
        {
          name  = "HOOKLANE_MOCK_SINK_MODE"
          value = "accept"
        },
        {
          name  = "HOOKLANE_MOCK_SINK_DELAY_SECONDS"
          value = "0"
        },
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2).close()\""]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      linuxParameters = {
        tmpfs = [{
          containerPath = "/tmp"
          size          = 16
          mountOptions  = ["noexec", "nosuid"]
        }]
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.service["mock-sink"].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])

  tags = {
    Name = "${local.name}-mock-sink"
  }
}

resource "aws_ecs_service" "api" {
  count = local.foundation_stage_enabled ? 1 : 0

  name                               = "${local.name}-api"
  cluster                            = aws_ecs_cluster.main[0].id
  task_definition                    = aws_ecs_task_definition.api[0].arn
  desired_count                      = local.runtime_service_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = var.fargate_platform_version
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60
  enable_execute_command             = false
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = [for subnet in aws_subnet.private : subnet.id]
    security_groups  = [aws_security_group.api[0].id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api[0].arn
    container_name   = "api"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.http]

  tags = {
    Name = "${local.name}-api"
  }
}

resource "aws_ecs_service" "worker" {
  count = local.foundation_stage_enabled ? 1 : 0

  name                               = "${local.name}-worker"
  cluster                            = aws_ecs_cluster.main[0].id
  task_definition                    = aws_ecs_task_definition.worker[0].arn
  desired_count                      = local.runtime_service_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = var.fargate_platform_version
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  enable_execute_command             = false
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = [for subnet in aws_subnet.private : subnet.id]
    security_groups  = [aws_security_group.worker[0].id]
    assign_public_ip = false
  }

  tags = {
    Name = "${local.name}-worker"
  }
}

resource "aws_ecs_service" "mock_sink" {
  count = local.foundation_stage_enabled ? 1 : 0

  name                               = "${local.name}-mock-sink"
  cluster                            = aws_ecs_cluster.main[0].id
  task_definition                    = aws_ecs_task_definition.mock_sink[0].arn
  desired_count                      = local.runtime_service_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = var.fargate_platform_version
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  enable_execute_command             = false
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = [for subnet in aws_subnet.private : subnet.id]
    security_groups  = [aws_security_group.mock_sink[0].id]
    assign_public_ip = false
  }

  service_registries {
    # The Cloud Map service publishes A records for awsvpc task ENIs. ECS only
    # accepts container name/port fields here when the DNS record type is SRV.
    registry_arn = aws_service_discovery_service.mock_sink[0].arn
  }

  tags = {
    Name = "${local.name}-mock-sink"
  }
}
