# Hooklane AWS deployment foundation

このdirectoryは、Hooklane v0.1.1を一つのAWS regionへ展開するためのTerraform deployment stageを定義する。remote state用bootstrap S3 bucket、artifact stageのECR repository／lifecycle policy、ECS serviceを0 taskに保つnetwork/foundation resourceは別の明示承認で作成・検証済みである。runtime resourceのdesired count 1へのapplyとtask runtime verificationは未実施である。

## 構成

- VPC、public subnet、private subnet、Internet Gateway
- NAT Gatewayは既定で無効。private subnetからAWS managed serviceへ接続するためのVPC endpointは既定で有効
- public ALBとAPI target group
- private ECS Fargate API、worker、controlled mock sink。foundation stageではserviceを0 taskに保つ
- Cloud Map private DNSによるmock sink service discovery。`awsvpc` taskのA recordを使うため、ECS service registryにはcontainer name/portを渡さない
- ECR repositoryをAPI、worker、mock sinkごとに作成
- ElastiCache Redis/Valkey。既定はTLS有効、single node、private subnet、public accessなし
- CloudWatch Logs groupとretention
- Secrets Manager。Redis URLはmanaged endpointから生成し、taskへ`secret`として注入する
- ECS execution roleと、application permissionを持たないtask role
- ECS deployment circuit breakerによるrollback

API taskはALB security groupからの8080だけを受信し、ALBのegressもAPI security groupの8080だけに限定する。workerとmock sinkはpublic inboundを持たず、RedisはAPI/worker security groupからの6379だけを受信する。全ECS taskの`assign_public_ip`はfalseである。workerの配送先はprivate Cloud Map mock sinkがdefaultで、承認済みのHTTP(S) endpointを`controlled_downstream_url`で切り替えられる。切替先へ到達するNATまたは別のegress経路は別途必要で、credential、query、fragmentを含むURLは受け付けない。

## Deployment stageとimage

既定の`image_tag`は`0.1.1`、`deployment_stage`の既定は`artifact`である。stageは次の3つだけを受け付ける。

- `artifact`: API、worker、mock sink用のECR repositoryとlifecycle policyだけを作成する。network、ALB、ElastiCache、VPC endpoint、ECS、IAM、Secrets Manager、CloudWatch Logsは作成しない。
- `foundation`: artifactを含む基盤resourceを作成し、ECS serviceのeffective desired countを0に保つ。taskは起動せず、image pullも始まらない。
- `runtime`: foundationを維持したまま、API、worker、controlled mock sinkを`desired_count`へ起動する。

image pushはこのrepositoryやCIから自動実行しない。ECRへはrelease tagではなく、build sourceを一意に特定する`git-<40-hex-commit>` tagを3 imageへ使う。そのdigestを確認した後だけ、`deployment_stage = "runtime"`をapplyする。

## Remote state bootstrap

このroot moduleはS3 backendを使う。backend bucket自体はroot moduleから自動作成せず、先に[`bootstrap`](bootstrap/README.md) moduleをlocal backendでplanし、人間承認の下で一度だけ用意する。

1. versioningを有効にしたprivate S3 bucket
2. default encryptionとpublic access block
3. state objectへの最小IAM権限をoperator側で付与する境界
4. `use_lockfile = true`によるS3 native state lock
5. retention、アクセス監査、削除保護の方針

bucket名やAWS credentialはrepositoryへ保存しない。[backend.hcl.example](backend.hcl.example)は値のない設定例である。

## Credential-free validation

AWS credentialを必要としないvalidationはrepository rootから実行する。

```bash
make terraform-validate
```

Terraform CLIがある環境では、このtargetは`terraform fmt -check -recursive`、`terraform init -backend=false`、`terraform validate`も実行する。CLIがない環境では、静的なresource、security、secret-output、cost-default contractだけを検証し、Terraform syntaxの実証は未実行として報告する。provider downloadが必要な場合もbackendやAWS APIは使用しない。

## Approved workflow

root moduleのS3 backendはbootstrap前にはplanしない。bootstrap bucketが承認済みの名前で作成された後、ignored `backend.hcl`を使ってbackendを初期化する。

```bash
cd infra
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

承認済みのremote stateを使う場合は、bootstrap済みbackend設定で次を実行する。

```bash
terraform init -backend-config=backend.hcl
terraform plan -var-file=terraform.tfvars -out=hooklane-dev.tfplan
```

## Staged deployment order

AWS resourceのapplyは人間承認された段階だけで実行する。順序は次のとおりである。

1. bootstrap moduleをplanし、Human承認後にbootstrap applyを行う。
2. 実際のbucket名を含むignored `backend.hcl`でroot S3 backendを初期化する。
3. `deployment_stage = "artifact"`でartifact applyを行い、3つのECR repositoryとlifecycle policyだけを作る。
4. API、worker、mock sink imageをbuildし、immutableな`git-<40-hex-commit>` tagをそれぞれのECR repositoryへpushする。
5. pushしたimage digestをECRでread-only確認する。
6. `deployment_stage = "foundation"`でfoundation applyを行う。ECS serviceのeffective desired countは0であり、image pullは開始しない。
7. `deployment_stage = "runtime"`でruntime applyを行う。`desired_count = 1`なら各serviceが1 taskになる。
8. ALB health check、API readiness、worker delivery、mock sink、CloudWatch Logsをruntime verificationする。
9. 必要時はroot moduleをdestroyし、bootstrap bucketはstate移行または不要判断後に別途destroyする。

`deployment_stage = "runtime"`をimage push前にapplyしてはならない。ALB ingressの`192.0.2.1/32`、bootstrap bucket name、Redis AUTH tokenはHuman input境界であり、自動生成・自動置換しない。

`terraform apply`はこのGoalの停止境界であり、明示的なHuman approvalがない限り実行しない。

## Cost-conscious defaults

- NAT Gateway: disabled
- artifact stage: ECR repositoryとimage storageだけ。network、ALB、ElastiCache、VPC endpoint、ECSは作成しない
- ECR lifecycle policy: repositoryごとに最新10 imageを保持する。image sizeとshared layerに依存するため、GB単位の上限はこのTerraformでは固定しない
- interface/gateway VPC endpoints: enabled to avoid NAT dependency; disable only when an approved egress route exists
- ElastiCache: one `cache.t4g.micro`
- ECS desired count: foundationではzero、image確認後のruntimeではone per service
- Container Insights: disabled
- CloudWatch Logs: seven-day retention
- ALB deletion protection: disabled for disposable dev
- Secrets Manager recovery window: zero for disposable dev only
- controlled downstream: private mock sink by default; external endpoint requires an explicit egress decision
- ALB ingress default: `192.0.2.1/32` documentation-only sentinel; replace with one Human-approved IPv4 `/32` before apply

ALB、ECS、ElastiCache、interface VPC endpointには継続課金が発生し得る。実行前にregion、保持期間、endpoint、NAT、cache node数を確認する。

## Destroy

destroyはstateとAWS resourceを確認した人間だけが実行する。詳細は[DESTROY.md](DESTROY.md)を参照する。
