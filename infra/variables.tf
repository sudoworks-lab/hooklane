variable "project_name" {
  description = "Short project name used in AWS resource names."
  type        = string
  default     = "hooklane"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project_name))
    error_message = "project_name must be 2-21 lowercase alphanumeric characters or hyphens."
  }
}

variable "environment" {
  description = "Environment name used in resource names and tags."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,12}$", var.environment))
    error_message = "environment must be lowercase alphanumeric characters or hyphens."
  }
}

variable "aws_region" {
  description = "AWS region for the deployment slice."
  type        = string
  default     = "ap-northeast-1"
}

variable "vpc_cidr" {
  description = "CIDR range for the deployment VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "availability_zone_count" {
  description = "Number of availability zones used by public and private subnets."
  type        = number
  default     = 2

  validation {
    condition     = var.availability_zone_count >= 2 && var.availability_zone_count <= 3
    error_message = "availability_zone_count must be between 2 and 3."
  }
}

variable "enable_nat_gateway" {
  description = "Create one NAT Gateway for private subnet egress. Disabled by default for cost control."
  type        = bool
  default     = false
}

variable "enable_vpc_endpoints" {
  description = "Create ECR, CloudWatch Logs, Secrets Manager, STS, and S3 VPC endpoints."
  type        = bool
  default     = true
}

variable "alb_ingress_cidr_blocks" {
  description = "CIDR ranges allowed to reach the public ALB HTTP listener. The default is a non-routable plan-only sentinel."
  type        = list(string)
  default     = ["192.0.2.1/32"]
}

variable "enable_https" {
  description = "Create an optional HTTPS listener in addition to the HTTP listener."
  type        = bool
  default     = false
}

variable "alb_certificate_arn" {
  description = "ACM certificate ARN used when enable_https is true."
  type        = string
  default     = null
  nullable    = true
}

variable "alb_deletion_protection" {
  description = "Enable ALB deletion protection. Keep false for a disposable dev environment."
  type        = bool
  default     = false
}

variable "controlled_downstream_url" {
  description = "Optional pre-approved HTTP(S) downstream URL for the worker. Null keeps the private mock sink default."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = var.controlled_downstream_url == null ? true : (
      can(regex("^https?://[^/?#]+", var.controlled_downstream_url)) &&
      !can(regex("@", var.controlled_downstream_url)) &&
      !can(regex("[?#]", var.controlled_downstream_url))
    )
    error_message = "controlled_downstream_url must be an HTTP(S) URL without credentials, query, or fragment."
  }
}

variable "image_tag" {
  description = "Application image tag that must be published before runtime services are enabled."
  type        = string
  default     = "0.1.1"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", var.image_tag)) && var.image_tag != "latest"
    error_message = "image_tag must be a bounded non-latest image tag."
  }
}

variable "runtime_services_enabled" {
  description = "Start ECS tasks only after the immutable application images have been pushed and verified in ECR."
  type        = bool
  default     = false
}

variable "desired_count" {
  description = "Desired count for API, worker, and controlled mock sink after runtime services are enabled."
  type        = number
  default     = 1

  validation {
    condition     = var.desired_count >= 1 && var.desired_count <= 4
    error_message = "desired_count must be between 1 and 4 for this vertical slice."
  }
}

variable "task_cpu" {
  description = "Fargate task CPU units shared by the three application task definitions."
  type        = number
  default     = 256
}

variable "task_memory" {
  description = "Fargate task memory in MiB shared by the three application task definitions."
  type        = number
  default     = 512
}

variable "fargate_platform_version" {
  description = "Fargate platform version used by all ECS services."
  type        = string
  default     = "1.4.0"
}

variable "cache_engine" {
  description = "ElastiCache engine. Valkey is the default Redis-compatible managed engine."
  type        = string
  default     = "valkey"

  validation {
    condition     = contains(["redis", "valkey"], var.cache_engine)
    error_message = "cache_engine must be redis or valkey."
  }
}

variable "cache_engine_version" {
  description = "ElastiCache engine version supported by the selected engine."
  type        = string
  default     = "7.2"
}

variable "cache_node_type" {
  description = "Cost-conscious ElastiCache node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "cache_num_nodes" {
  description = "Number of cache nodes. One is the default; increase only with an HA decision."
  type        = number
  default     = 1

  validation {
    condition     = var.cache_num_nodes >= 1 && var.cache_num_nodes <= 3
    error_message = "cache_num_nodes must be between 1 and 3."
  }
}

variable "enable_cache_multi_az" {
  description = "Enable ElastiCache Multi-AZ. Requires a deliberate HA and cost decision."
  type        = bool
  default     = false
}

variable "cache_apply_immediately" {
  description = "Apply ElastiCache changes immediately. Keep false for controlled changes."
  type        = bool
  default     = false
}

variable "redis_auth_token" {
  description = "Optional sensitive Redis AUTH token. Supply outside source control; never output it."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true

  validation {
    condition     = var.redis_auth_token == null ? true : length(var.redis_auth_token) >= 16
    error_message = "redis_auth_token must be at least 16 characters when provided."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "log_retention_days must be an AWS-supported retention value."
  }
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window. Zero is suitable only for disposable dev environments."
  type        = number
  default     = 0

  validation {
    condition     = var.secret_recovery_window_days >= 0 && var.secret_recovery_window_days <= 30
    error_message = "secret_recovery_window_days must be between 0 and 30."
  }
}

variable "enable_container_insights" {
  description = "Enable ECS Container Insights, which increases observability cost."
  type        = bool
  default     = false
}

variable "ecr_force_delete" {
  description = "Allow ECR repositories with images to be deleted during destroy. Keep false by default."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
