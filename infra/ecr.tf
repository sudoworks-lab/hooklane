resource "aws_ecr_repository" "application" {
  for_each = local.workload_names

  name                 = "${local.name}/${each.value}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.ecr_force_delete

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name      = "${local.name}-${each.value}"
    Component = each.value
  }
}

resource "aws_ecr_lifecycle_policy" "application" {
  for_each = aws_ecr_repository.application

  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the most recent ten images for the interview slice."
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = {
        type = "expire"
      }
    }]
  })
}
