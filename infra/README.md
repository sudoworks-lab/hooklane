# Hooklane AWS deployment foundation

このdirectoryは、Hooklane v0.1.1を一つのAWS regionへ展開するためのTerraform foundationを定義する。AWS resourceの作成、変更、削除はこの作業では実行していない。

## 構成

- VPC、public subnet、private subnet、Internet Gateway
- NAT Gatewayは既定で無効。private subnetからAWS managed serviceへ接続するためのVPC endpointは既定で有効
- public ALBとAPI target group
- private ECS Fargate API、worker、controlled mock sink
- Cloud Map private DNSによるmock sink service discovery
- ECR repositoryをAPI、worker、mock sinkごとに作成
- ElastiCache Redis/Valkey。既定はTLS有効、single node、private subnet、public accessなし
- CloudWatch Logs groupとretention
- Secrets Manager。Redis URLはmanaged endpointから生成し、taskへ`secret`として注入する
- ECS execution roleと、application permissionを持たないtask role
- ECS deployment circuit breakerによるrollback

API taskはALB security groupからの8080だけを受信する。workerとmock sinkはpublic inboundを持たず、RedisはAPI/worker security groupからの6379だけを受信する。全ECS taskの`assign_public_ip`はfalseである。workerの配送先はprivate Cloud Map mock sinkがdefaultで、承認済みのHTTP(S) endpointを`controlled_downstream_url`で切り替えられる。切替先へ到達するNATまたは別のegress経路は別途必要で、credential、query、fragmentを含むURLは受け付けない。

## Versionとimage

既定の`image_tag`は`0.1.1`。ECR repositoryは作成するが、image pushはこのrepositoryやCIから自動実行しない。ECRへimageをpushし、taskを起動できる状態にすることは、AWS apply前の別のhuman-approved operationである。

## Remote state bootstrap

このroot moduleはS3 backendを使う。backend bucket自体はroot moduleから自動作成せず、先に[`bootstrap`](bootstrap/README.md) moduleをlocal backendでplanし、人間承認の下で一度だけ用意する。

1. versioningを有効にしたprivate S3 bucket
2. default encryptionとpublic access block
3. state objectへの最小IAM権限
4. `use_lockfile = true`によるS3 state lock
5. retention、アクセス監査、削除保護の方針

bucket名やAWS credentialはrepositoryへ保存しない。[backend.hcl.example](backend.hcl.example)は値のない設定例である。

## Credential-free validation

AWS credentialを必要としないvalidationはrepository rootから実行する。

```bash
make terraform-validate
```

Terraform CLIがある環境では、このtargetは`terraform fmt -check -recursive`、`terraform init -backend=false`、`terraform validate`も実行する。CLIがない環境では、静的なresource、security、secret-output、cost-default contractだけを検証し、Terraform syntaxの実証は未実行として報告する。provider downloadが必要な場合もbackendやAWS APIは使用しない。

## Approved workflow

```bash
cd infra
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
terraform plan -out=hooklane-dev.tfplan
```

remote stateを使う場合は、bootstrap済みbackend設定で次を実行する。

```bash
terraform init -backend-config=backend.hcl
terraform plan -out=hooklane-dev.tfplan
```

`terraform apply`はこのGoalの停止境界であり、`AWS_APPLY_APPROVED`が明示されるまで実行しない。

## Cost-conscious defaults

- NAT Gateway: disabled
- interface/gateway VPC endpoints: enabled to avoid NAT dependency; disable only when an approved egress route exists
- ElastiCache: one `cache.t4g.micro`
- ECS desired count: one per service
- Container Insights: disabled
- CloudWatch Logs: seven-day retention
- ALB deletion protection: disabled for disposable dev
- Secrets Manager recovery window: zero for disposable dev only
- controlled downstream: private mock sink by default; external endpoint requires an explicit egress decision
- ALB ingress default: `192.0.2.1/32` documentation-only sentinel; replace with one Human-approved IPv4 `/32` before apply

ALB、ECS、ElastiCache、interface VPC endpointには継続課金が発生し得る。実行前にregion、保持期間、endpoint、NAT、cache node数を確認する。

## Destroy

destroyはstateとAWS resourceを確認した人間だけが実行する。詳細は[DESTROY.md](DESTROY.md)を参照する。
