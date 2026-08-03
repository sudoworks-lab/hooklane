terraform {
  required_version = ">= 1.15.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 5.95.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(
      {
        Project   = "hooklane"
        ManagedBy = "terraform"
        Component = "terraform-state-bootstrap"
      },
      var.tags,
    )
  }
}
