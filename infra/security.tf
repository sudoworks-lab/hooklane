resource "aws_security_group" "alb" {
  count = local.foundation_stage_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition = var.deployment_stage != "runtime" || alltrue([
        for cidr in var.alb_ingress_cidr_blocks : !contains(
          ["192.0.2.1/32", "0.0.0.0/0", "::/0"],
          cidr,
        )
      ])
      error_message = "runtime ALB ingress requires a Human-approved CIDR and must not use the sentinel or an unrestricted CIDR."
    }
  }

  name        = "${local.name}-alb"
  description = "Public ALB ingress for Hooklane API only."
  vpc_id      = aws_vpc.main[0].id

  dynamic "ingress" {
    for_each = var.alb_ingress_cidr_blocks

    content {
      description      = "HTTP from configured ALB ingress CIDR"
      from_port        = 80
      to_port          = 80
      protocol         = "tcp"
      cidr_blocks      = [ingress.value]
      ipv6_cidr_blocks = []
    }
  }

  egress {
    description     = "ALB to API target"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.api[0].id]
  }

  tags = {
    Name = "${local.name}-alb"
  }
}

resource "aws_security_group_rule" "alb_https" {
  count = local.foundation_stage_enabled && var.enable_https ? 1 : 0

  type              = "ingress"
  security_group_id = aws_security_group.alb[0].id
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = var.alb_ingress_cidr_blocks
  description       = "HTTPS from configured ALB ingress CIDR"
}

resource "aws_security_group" "api" {
  count = local.foundation_stage_enabled ? 1 : 0

  name        = "${local.name}-api"
  description = "ECS API tasks accept traffic from the ALB security group only."
  vpc_id      = aws_vpc.main[0].id

  egress {
    description = "API egress for Redis and controlled downstream changes"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name}-api"
  }
}

resource "aws_security_group_rule" "alb_to_api" {
  count = local.foundation_stage_enabled ? 1 : 0

  type                     = "ingress"
  security_group_id        = aws_security_group.api[0].id
  source_security_group_id = aws_security_group.alb[0].id
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  description              = "ALB to API"
}

resource "aws_security_group" "worker" {
  count = local.foundation_stage_enabled ? 1 : 0

  name        = "${local.name}-worker"
  description = "ECS worker tasks have no public or service ingress."
  vpc_id      = aws_vpc.main[0].id

  egress {
    description = "Worker egress for Redis, mock sink, and controlled downstream"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name}-worker"
  }
}

resource "aws_security_group" "mock_sink" {
  count = local.foundation_stage_enabled ? 1 : 0

  name        = "${local.name}-mock-sink"
  description = "Controlled mock sink accepts traffic from worker tasks only."
  vpc_id      = aws_vpc.main[0].id

  ingress {
    description     = "Worker to controlled mock sink"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.worker[0].id]
  }

  egress {
    description = "Mock sink response and health traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name}-mock-sink"
  }
}

resource "aws_security_group" "redis" {
  count = local.foundation_stage_enabled ? 1 : 0

  name        = "${local.name}-redis"
  description = "ElastiCache accepts Redis traffic from API and worker tasks only."
  vpc_id      = aws_vpc.main[0].id

  ingress {
    description     = "API to Redis"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.api[0].id]
  }

  ingress {
    description     = "Worker to Redis"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.worker[0].id]
  }

  tags = {
    Name = "${local.name}-redis"
  }
}

resource "aws_security_group" "vpc_endpoints" {
  count = local.foundation_stage_enabled ? 1 : 0

  name        = "${local.name}-vpc-endpoints"
  description = "Private interface endpoints accept HTTPS from private subnet CIDRs."
  vpc_id      = aws_vpc.main[0].id

  dynamic "ingress" {
    for_each = local.private_subnet_cidrs

    content {
      description = "Private subnet to VPC endpoint"
      from_port   = 443
      to_port     = 443
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }

  egress {
    description = "Endpoint response traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name}-vpc-endpoints"
  }
}
