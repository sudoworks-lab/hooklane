# Hooklane 検証根拠

## 対象

この文書はHooklaneの公開済みsource snapshotと公開mainに対する技術的な検証境界を記録する。reproducibleなrepository contractと検証結果の要約であり、運用履歴、cloud productionの認定、security certificationではない。

## feature受け入れ

- `features.json`のF001〜F029は29/29で`passes: true`
- blocked feature countは0
- featureのdescriptionとverification stepはmachine-readableなacceptance contractとして維持する

## quality gate

`make verify`はsyntax／configuration check、Ruff、strict mypy、unit／integration test、security scan、Helm／Kubernetes validation、文書contractを集約する。全ての構成commandがsuccessであることを受け入れ条件とする。

`make terraform-validate`はTerraform foundationのresource、security group、secret output、cost default、rollback contractをcredential-freeで検証する。Terraform CLIがない環境ではHCL syntaxのCLI検証を行わず、AWS APIにも接続しない。

## runtime検証

### Compose

`make demo-smoke`はlocal image build、service health、event受付、非同期配送、status参照、metrics、project専用resourceのcleanupを確認する。

### kind E2E

`make e2e-kind`は固定したkindとHelm構成で、正常配送、idempotency、retry、pending recovery、status参照、cleanupを確認する。

### rolling updateとrollback

`make rollout-smoke`はavailableなrolling update、bounded worker drain、意図的なbad revisionの拒否、Helm rollback、復旧を確認する。

### observability

`make observability-smoke`はPrometheus targetとmetric、Grafana provisioning、SLI query、alert rule、Runbook参照、障害signal、復旧を確認する。

### incident drill

`make incident-smoke`はdownstream 5xx、Redis outage、worker stopを対象に、検知、復旧、accepted eventの整合を確認する。

### clean-room

`make clean-room`はtracked Git candidateからhardlinkなしの一時cloneを作り、initialization、dependency setup、verify、Compose、kind E2E、rollout、observability、incident、documentation、diff、cleanup contractを実行する。

## GitHub Actions

- GitHub hosted Actionsは公開mainで実行済み
- Quality, security, and chart gatesはsuccess
- kind delivery and recovery E2Eはsuccess
- success時のfailure diagnostics uploadはskip、cleanupはsuccess
- Node.js 20 deprecation annotationは現行workflowで発生していない

Hosted CIは現在の公開mainに対する自動検証であり、cloud productionや本番trafficの実績ではない。

## security scan

受け入れたlocal gateの結果は次の通り。

- Gitleaks: scanned Git historyとworking treeでsecret findingなし
- OSV-Scanner: `requirements.lock`にknown vulnerabilityなし
- Trivy filesystemとlocal buildしたAPI、worker、mock-sink image: repository policy上のHIGH／CRITICAL findingなし

scanner databaseとupstream advisoryは変化する。結果は検証したsnapshotの事実であり、後続revisionでは再実行が必要となる。

## 実証済みの事実

- localのquality、security、documentation、Helm、Compose、kind、rollout、observability、incident contractはpass
- 配送はat-least-onceであり、downstreamへのattemptが重複し得る
- 検証した構成はsingle-node kind、single Redis、repository内mock sink
- runtime検証はlocal buildしたapplication imageと固定済みupstream imageを使う
- v0.1.1のtagがcurrent source baseline。GitHub Releaseの有無はこのsource contractでは主張しない
- `platform/aws-interview-v1`ではTerraform 1.15.5、AWS provider lock、bootstrap plan、ECS serviceを0 taskに保つfoundation plan、各serviceを1 taskにするruntime planをread-onlyで確認した。remote state bootstrap S3 bucket、artifact stage、ECS serviceを0 taskに保つfoundation stageは明示承認の下で作成・security contractを検証済みである
- 同branchの`artifact` stageはread-only planで`create=6`、`update=0`、`delete=0`を確認後、同じexact planをapplyした。対象はAPI、worker、mock sinkのECR repository各1件とlifecycle policy各1件だけであり、tag immutability、AES256 encryption、scan-on-push、repository policyなし、最新10 imageのlifecycle policyをread-onlyで確認した
- 承認済みcommit固定tagのAPI、worker、mock sink imageをprivate ECRへpushし、digestとsizeをGit管理外evidenceへ保存した。local Trivy image policyのHIGH／CRITICAL findingは3 imageとも0件である。approved tagだけにmanual ECR Basic Scanを要求したが、3 imageとも`UNSUPPORTED`でseverity countは未確定である
- 同じcommit固定tagで生成した`foundation` stageのread-only planは`create=49`、`update=0`、`delete=0`で、既存ECR 6 resourceのmutationは0、API／worker／mock sinkのECS desired countは全て0である。明示承認の下でapplyし、途中でCloud Map A recordに不要なECS container name/portを指定したためmock sink serviceだけが`InvalidParameter`で失敗した。A record contractに合わせてその指定を削除し、最終的なfoundation convergence planが`create=0`、`update=0`、`delete=0`となることを確認した
- foundationの実AWS構成はVPC、public/private subnet各2、NAT Gatewayなし、private DNS有効なinterface VPC endpoint 5件（ENI 10件）、S3 gateway endpoint、ALB、Valkey 7.2系 single node、CloudWatch Logs 3 group、Secrets Manager secret、IAM、Cloud Map、ECS service 3件を含む。ALB ingressは`192.0.2.1/32` sentinelだけで、ECS serviceは全て`desired/running/pending = 0/0/0`、running/stopped Fargate taskは0件である
- 2026-07-27 JSTに、Human承認済みの単一IPv4 `/32`だけをALB ingressへ設定した`runtime` stageを実行した。最初のexact planは`create=0`、`update=4`、`delete=0`で、API、worker、mock sinkのdesired countを0から1へ変え、ALB ingress sentinelを置換するものだった。同じartifactのapply後、ALB security groupにAPI targetへのegressがないことを確認し、API security groupのTCP/8080だけへ限定したegress ruleを追加する`create=0`、`update=1`、`delete=0`のexact planをapplyした
- egress修正後、ALB target、API task、mock sink taskはhealthyになった。一方workerはECS startup health checkのfailureで置換を繰り返し、stableな`desired/running/pending = 1/1/0`へ収束しなかった。structured CloudWatch Logsには`worker_started`と`worker_stopped`だけが記録され、application errorを根拠にした原因特定はできなかった。このためnormal delivery、idempotency、retry、dead-letter、pending recovery、graceful shutdown、deployment rollback drillは実行していない
- cost controlのためECS service 3件を`desired_count = 0`へ戻す`create=0`、`update=3`、`delete=0`のexact planをapplyした。その後artifact stageへのcleanup plan（`create=0`、`update=0`、`delete=49`）をapplyし、ALB、Valkey、VPC endpoint、VPC、ECS、Cloud Map、IAM、Secrets Manager、CloudWatch Logsを削除した。post-cleanupのactual backend artifact planは`create=0`、`update=0`、`delete=0`である。remote state bootstrap S3 bucket、ECR repository 3件、lifecycle policy 3件、approved image 3件は保持されている

## 未確認事項

- cloud production、実在する外部downstream、multi-node／multi-zone availability、long-running load、本番traffic
- rolling 30日のSLO達成実績
- production Alertmanager、notification destination、on-call運用
- AWS runtimeのnormal delivery、idempotency、retry、dead-letter、pending recovery、graceful shutdown、deployment rollback drill、approved ALB ingressからの外部到達
- worker ECS startup health check failureの原因、AWS runtime上のworker／Valkey接続の安定性、ECR Basic scanのseverity結果

## 配布範囲

Hooklaneはsource-onlyで配布する。source code、Dockerfile、Helm chart、configuration、documentation、検証手順を含む。prebuilt container image、container registry、release artifact、binary distributionは配布しない。application imageはlocal buildし、第三者dependencyとupstream imageには各上流のlicenseとnoticeが適用される。
