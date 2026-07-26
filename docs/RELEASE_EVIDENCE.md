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
- `platform/aws-interview-v1`ではTerraform 1.15.5、AWS provider lock、bootstrap plan、ECS serviceを0 taskに保つfoundation plan、各serviceを1 taskにするruntime planをread-onlyで確認した。remote state bootstrap S3 bucketだけは明示承認の下で作成・security contractを検証済みである
- 同branchの`artifact` stageはread-only planで`create=6`、`update=0`、`delete=0`を確認後、同じexact planをapplyした。対象はAPI、worker、mock sinkのECR repository各1件とlifecycle policy各1件だけであり、tag immutability、AES256 encryption、scan-on-push、repository policyなし、最新10 imageのlifecycle policyをread-onlyで確認した
- 承認済みcommit固定tagのAPI、worker、mock sink imageをprivate ECRへpushし、digestとsizeをGit管理外evidenceへ保存した。local Trivy image policyのHIGH／CRITICAL findingは3 imageとも0件である。ECR Basic scan結果は照会時点でpendingであり、severity countは未確定である
- 同じcommit固定tagで生成した`foundation` stageのread-only planは`create=49`、`update=0`、`delete=0`で、既存ECR 6 resourceのmutationは0、API／worker／mock sinkのECS desired countは全て0である

## 未確認事項

- cloud production、実在する外部downstream、multi-node／multi-zone availability、long-running load、本番traffic
- rolling 30日のSLO達成実績
- production Alertmanager、notification destination、on-call運用
- foundation／runtime AWS apply、cloud runtime、destroy実行、ECR Basic scanの完了結果

## 配布範囲

Hooklaneはsource-onlyで配布する。source code、Dockerfile、Helm chart、configuration、documentation、検証手順を含む。prebuilt container image、container registry、release artifact、binary distributionは配布しない。application imageはlocal buildし、第三者dependencyとupstream imageには各上流のlicenseとnoticeが適用される。
