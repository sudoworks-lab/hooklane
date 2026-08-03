# Hooklane remote state bootstrap

このmoduleはHooklane Terraform root module自身が利用するS3 remote state bucketを定義する。backendのchicken-and-eggを避けるため、このmoduleはlocal backendのままplanする。

含まれるsecurity boundary:

- S3 Bucket Versioning
- SSE-S3 AES256 server-side encryption
- S3 Public Access Block
- BucketOwnerEnforced ownership
- insecure transportを拒否するbucket policy
- `force_destroy = false`
- state lockはroot moduleのS3 backend `use_lockfile = true`で行い、DynamoDB tableは作成しない。S3 Bucket KeyはSSE-KMS用のため、SSE-S3 bucketには設定しない

`bucket_name`はexampleのままapplyせず、globally uniqueなHuman承認済み名称へ置換する。bucketはbootstrap moduleのdestroy対象だが、Hooklane dev environmentのdestroyでは保持する。

```bash
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -out=bootstrap.tfplan
```

必要なoperator権限は、bucket、versioning、encryption、public access block、ownership controls、bucket policyを作成・参照できる権限である。実行者のIAM変更はこのmoduleでは行わない。
