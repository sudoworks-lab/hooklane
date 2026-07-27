# Hooklane 検証根拠

## 対象

この文書はHooklaneの公開済みsource snapshotと公開mainに対する技術的な検証境界を記録する。reproducibleなrepository contractと検証結果の要約であり、運用履歴、cloud productionの認定、security certificationではない。

## feature受け入れ

- `features.json`のF001〜F029は29/29で`passes: true`
- blocked feature countは0
- featureのdescriptionとverification stepはmachine-readableなacceptance contractとして維持する

## quality gate

`make verify`はsyntax／configuration check、Ruff、strict mypy、unit／integration test、security scan、Helm／Kubernetes validation、文書contractを集約する。全ての構成commandがsuccessであることを受け入れ条件とする。

`make terraform-validate`はTerraform foundationのresource、security group、secret output、cost default、rollback contractをcredential-freeで検証する。通常のlocal環境でTerraform CLIがない場合は`[degraded]`と表示し、CIではTerraform 1.15.5を公式checksum検証付きで導入したうえでCLI検証を必須にする。AWS APIには接続しない。

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

## AWS evidence scope

sanitized AWS evidenceのsource commitは`50af2be9d0cc0e6a61ab8ab8a53f924aa7d8fc7e`、image source commitは`5a2c3cd7e99fda46b9622abea30e40eb4c91dca9`である。現在HEADのapplication / Helm / Terraform修正はlocal verification済みだが、現在HEADおよび新immutable imageはAWS再検証前であり、記録済みevidenceを現在HEADのAWS実証とは扱わない。

## GitHub Actions

- GitHub hosted Actionsは公開mainの旧baselineで実行済み。現在branchはPush後のPR CIで確認する
- Quality, security, and chart gatesはsuccess
- kind delivery and recovery E2Eはsuccess
- success時のfailure diagnostics uploadはskip、cleanupはsuccess
- Node.js 20 deprecation annotationは現行workflowで発生していない

Hosted CIは公開mainの旧baselineに対する自動検証であり、cloud productionや本番trafficの実績ではない。

## security scan

受け入れたlocal gateの結果は次の通り。

- Gitleaks: scanned Git historyとworking treeでsecret findingなし
- OSV-Scanner: `requirements.lock`にknown vulnerabilityなし
- Trivy filesystemとlocal buildしたAPI、worker、mock-sink image: repository policy上のHIGH／CRITICAL findingなし

scanner databaseとupstream advisoryは変化する。結果は検証したsnapshotの事実であり、後続revisionでは再実行が必要となる。

## 実証済みの事実

- localのquality、security、documentation、Helm、Compose、kind、rollout、observability、incident contractはpass
- 修正後AWS runtimeのsanitized machine-readable evidenceは[`docs/aws/runtime-evidence.json`](aws/runtime-evidence.json)に保存し、SHA-256は`a3d81f8e186a8be7386fe5fe091e1285c971fb41107fa9fe00881b90a61ff8ff`である。account metadata、ARN、credential、Redis URL、payload、Idempotency-Key生値は含めない
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
- 初回runtimeでは修正前のECS startup health contractがworker replacementを引き起こした。ローカルDocker再現で、Redis接続不能時にもworker processと`9090` metrics surfaceは生存する一方、旧health commandだけがnon-zeroになることを確認し、worker livenessをlocal metrics probeへ修正した。この初回failureは後続の修正後runtimeで解消を確認した。
- cost controlのためECS service 3件を`desired_count = 0`へ戻す`create=0`、`update=3`、`delete=0`のexact planをapplyした。その後artifact stageへのcleanup plan（`create=0`、`update=0`、`delete=49`）をapplyし、ALB、Valkey、VPC endpoint、VPC、ECS、Cloud Map、IAM、Secrets Manager、CloudWatch Logsを削除した。post-cleanupのactual backend artifact planは`create=0`、`update=0`、`delete=0`である。remote state bootstrap S3 bucket、ECR repository 3件、lifecycle policy 3件、approved image 3件は保持されている
- 2026-07-27 JSTにG2.9のworker liveness修正を含むfoundationを再作成した。保存済みexact planは`create=49`、`update=0`、`delete=0`であり、apply wrapperのreceiptは保存されなかったが、Terraform processの完了後にactual backendをrefreshしたconvergence planが`create=0`、`update=0`、`delete=0`となることを確認した。実AWSでVPC、ALB、Valkey、private DNS有効なinterface VPC endpoint 5件（ENI 10件）、S3 gateway endpoint、CloudWatch Logs 3 group、Secrets Manager secret、Cloud Map、IAM、ECS service 3件を確認した
- 修正後worker task definitionは`CMD-SHELL`のlocalhost metrics probeを使い、`interval=30`、`timeout=5`、`retries=3`、`startPeriod=30`である。foundationではAPI／worker／mock sinkを全て`desired/running/pending = 0/0/0`に保ち、runtime planはtask definition、image tag、ECR、network、ALB、Valkey、IAM、secretの置換を含まないことを確認した。
- 2026-07-27 JSTに、Human承認済みの単一IPv4 `/32`を使う修正後の`runtime` stageを実行した。保存済みexact planは`create=0`、`update=4`、`delete=0`で、API／worker／mock sinkのdesired countを`0`から`1`へ変更し、ALB ingressをsentinelから承認済みCIDRへ置換するものだった。apply後40秒で3 serviceは全て`desired/running/pending = 1/1/0`、worker container healthはhealthy、API targetはhealthyとなった。既存のcommit固定ECR imageを再利用し、新しいimage buildまたはpushは行っていない
- 同runtimeではALB経由のdummy eventが`202 Accepted`後にattempt 1で`delivered`となること、同一idempotency keyと同一requestが同じevent IDを返すこと、同一keyと異なるrequestが`409 Conflict`となることを確認した。event payload、idempotency keyの生値、credential、Redis URLはevidenceへ保存していない
- controlled mock sinkを既存の`server_error` modeへ一時的に切り替え、HTTP 503に対するretry schedule、attempt増加、attempt 5での`dead_letter`を確認した。正常modeへ戻した後の新規eventはattempt 1で`delivered`となった。一方、同一retry eventが途中でsuccessへ遷移する経路はこの実行では確認していない
- worker serviceのforce-new-deploymentでSIGTERMを伴うreplacementを確認し、CloudWatch Logsで`worker_stopped`とreplacement後の`worker_started`を確認した。in-flight messageを停止中にclaimして終端状態へ進めるpending recoveryは、このAWS runtimeでは未実証である
- APIだけへ存在しないimage tagのtemporary task definitionを適用するrollback drillを実行した。deployment circuit breakerはimage pull failureを検出し、既存healthy targetは維持された。ただしECSが旧revisionへ自動復帰し切らなかったため、approved revisionへの安全な手動復帰を行った。automatic rollbackの成功は実証済みとは扱わない
- runtime検証後、artifact stageへのexact cleanup plan（`create=0`、`update=0`、`delete=49`）をapplyし、483.391秒で完了した。post-cleanup artifact convergence planは`create=0`、`update=0`、`delete=0`である。read-only確認ではALB、Valkey、Hooklane VPC endpoint、VPC、CloudWatch Logs、Secrets Manager secret、IAM role、Cloud Map、active ECS cluster、ECS service、running/pending task、active task definitionは0件であり、ECR repository 3件、lifecycle policy 3件、approved image 3件、remote state S3 bucketは保持されている。ECS `describe-clusters`には課金・service・taskを伴わない`INACTIVE` tombstoneが1件残る

## 未確認事項

- cloud production、実在する外部downstream、multi-node／multi-zone availability、long-running load、本番traffic
- rolling 30日のSLO達成実績
- production Alertmanager、notification destination、on-call運用
- AWS runtimeでの同一retry eventのeventual success、pending recovery、in-flight workのgraceful shutdown、deployment circuit breakerによるautomatic rollback
- 修正後のAWS runtimeにおけるworker／Valkey接続のlong-running stability、ECR Basic scanのseverity結果

## 配布範囲

Hooklaneはsource-onlyで配布する。source code、Dockerfile、Helm chart、configuration、documentation、検証手順を含む。prebuilt container image、container registry、release artifact、binary distributionは配布しない。application imageはlocal buildし、第三者dependencyとupstream imageには各上流のlicenseとnoticeが適用される。
