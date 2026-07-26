variable "aws_region" {
  description = "AWS region for the remote state bucket."
  type        = string
  default     = "ap-northeast-1"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name. Replace the plan-only example before apply."
  type        = string
  default     = "hooklane-dev-terraform-state-plan-only"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be a valid lowercase S3 bucket name."
  }
}

variable "force_destroy" {
  description = "Allow deletion of non-empty state bucket. Keep false."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags for the bootstrap bucket."
  type        = map(string)
  default     = {}
}
