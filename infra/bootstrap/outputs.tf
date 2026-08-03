output "bucket_name" {
  description = "Remote state bucket name."
  value       = aws_s3_bucket.state.bucket
}

output "bucket_arn" {
  description = "Remote state bucket ARN."
  value       = aws_s3_bucket.state.arn
}

output "backend_key_example" {
  description = "Example key used by the Hooklane dev backend."
  value       = "hooklane/dev/terraform.tfstate"
}
