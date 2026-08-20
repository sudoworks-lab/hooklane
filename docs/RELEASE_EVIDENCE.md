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

source commit `123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b` に対するAWS revalidation evidenceはimmutable image tag `git-123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b`を使用し、main runのsource_run_idは`20260802T154822Z`、cleanup recovery/canonical reconstruction runのcleanup_recovery_run_idは`20260802T160316Z`、verdictは`PASS_AND_CLEAN`である。foundation 49/0/0、runtime 0/3/0、cleanup 0/0/49、smoke 4/4を確認した。image proofはAPI/workerが`configuration_backed`、mock-sinkが`direct_plan`。final state 6、charge-heavy 0、ECR repository 3、ECS service/task 0、INACTIVE tombstone、apply process terminatedである。receipt SHA-256は`a1aa6f342f6b052525feba59afc6bef961b11b58b82804a37f6e34c3d305922e`、diagnostic SHA-256は`27b8d08b4c13af84090b7927d2d86a8fc6acce243e6bf7e4153827aadedaa4bd`である。

## GitHub Actions

PR #1のPR HEADは`f7d2db9822215ecb8ca81e335982fb47a5c019e8`であり、PR titleは`feat: add AWS-validated Hooklane deployment and safety contracts`である。これに対するHosted CI Run #9 / Run ID `30791958394`はsuccessだった。

- Quality, security, and chart gatesはsuccess
- kind delivery and recovery E2Eはsuccess
- PR #1はmerge commit `9c342097a654c4f7f29e6c548c5870c30d7e7d8a`でmainへmerge済み
- merge commit固有のpush-triggered CI結果は、tracked evidence上で独立確認済みとは扱わない
- success時のfailure diagnostics uploadはskip、cleanupはsuccess
- Node.js 20 deprecation annotationは現行workflowで発生していない

Hosted CIはcloud production、本番traffic、AWS runtimeの証拠ではない。AWS revalidationのsource commitと証拠境界は、上記のAWS evidence scopeおよび[`docs/aws/runtime-evidence.json`](aws/runtime-evidence.json)を正本とし、current main自体をAWS-tested sourceとは扱わない。

## security scan

受け入れたlocal gateの結果は次の通り。

- Gitleaks: scanned Git historyとworking treeでsecret findingなし
- OSV-Scanner: `requirements.lock`にknown vulnerabilityなし
- Trivy filesystemとlocal buildしたAPI、worker、mock-sink image: repository policy上のHIGH／CRITICAL findingなし

scanner databaseとupstream advisoryは変化する。結果は検証したsnapshotの事実であり、後続revisionでは再実行が必要となる。

## 実証済みの事実

- localのquality、security、documentation、Helm、Compose、kind、rollout、observability、incident contractはpass
- source commit `123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b`に対するAWS revalidationのsanitized machine-readable evidenceは[`docs/aws/runtime-evidence.json`](aws/runtime-evidence.json)に保存する。main/recovery artifactのSHA-256 provenanceとcanonical evidence/manifestのSHA-256を同JSONに記録し、account ID、ARN、credential、token、endpoint、registry hostname、payload、Idempotency-Key生値、個人情報は含めない
- 配送はat-least-onceであり、downstreamへのattemptが重複し得る
- 検証した構成はsingle-node kind、single Redis、repository内mock sink
- runtime検証はlocal buildしたapplication imageと固定済みupstream imageを使う
- v0.1.1のtagがcurrent source baseline。GitHub Releaseの有無はこのsource contractでは主張しない
- `foundation` planは49/0/0、apply/readinessはPASSである。`runtime` planは0/3/0、apply/healthはPASSである。runtime smokeは4/4 PASSで、項目はnormal_delivery、idempotency_same、idempotency_conflict、aggregate passedである。image proof modeはAPIとworkerが`configuration_backed`、mock-sinkが`direct_plan`である
- `cleanup` planは0/0/49、cleanupはPASSである。final state 6 resources、ECR repository 3、ECR lifecycle policy 3、required image 3を保持し、charge-heavy 0、ECR digest/contract match、ECS service/task 0、INACTIVE tombstone 1、apply process terminatedである
- receipt/diagnosticの不整合はapplication障害ではない。旧validatorがdelete planの`after=null`を`.get()`してAttributeErrorになったこと、cleanup-recoveryが本体receipt/diagnosticをロードせず初期false/nullを残したこと、diagnosticのcleanup statusを更新しなかったことがconfirmed root causeである
- current harnessのfixtureは既存v1.4 85件と今回追加20件が全PASSで、nullable plan fallback、cleanup `after=null`、full-success coherence、failure/recovery atomic receipt、redactionを検証した

## post-P0 Cloudflare local spike

2026-08-20のbaseline HEAD `aa0d413b4255899bb29842e7f38b74e6f04a7c08`から始めたuncommitted current working treeで、既存Redis backendを変更せず`cloudflare/`へlocal-only spikeを分離した。この節はv0.1.1 tag、Hosted CI、cloud productionの根拠を置き換えない。

- `make cloudflare-check`: Cloudflare-specific failure/configuration tests 27件、Cloudflare sourceのRuff、strict mypy 6 files、Wrangler 4.124.0/workerd local flowがsuccess
- local flow: default `SPIKE_TEST_MODE=false`ではlocal-only interfaceが404、明示override後のliveness 200／D1 readiness 200、normal deliveryは`delivered`／attempt 1、20 concurrent same-key requestは1 logical event、conflictは409、D1 acceptance faultとpayload chunk transaction faultは503、Queue send fault後の20 concurrent outbox repairは1 dispatch／send count 1／`delivered`、outbox duplicateはterminal suppressionでattempt 1を維持、delivery transition faultは同一eventのsink calls 2／attempt 2、dead-letter／delivered terminal redeliveryはsinkを再実行せず、stale failure／successはcurrent stateを上書きせず、2,065,536-byte payloadはD1 chunkから`delivered`、mock sink 503継続時はattempt 5で`dead_letter`
- root `make test-unit`: 201 passed。Cloudflare public request schema、status vocabulary、Redis／Cloudflare portfolio comparisonのcontract testを含む
- root `make test-integration`: isolated disposable Redisを使う51件がpassedし、test container cleanupがsuccess
- root `make lint`: success
- root `make typecheck`: strict mypy 104 source filesでsuccess
- `make docs-check`: success
- `git diff --check`: success
- `make security-secret`: Git historyとworking treeのsecret findingは0
- Durable ObjectsとR2は不採用。D1 unique constraintでconcurrent idempotencyを満たし、Queueへpayloadではなく`event_id`だけを送る。payloadは1.5 MB以下のD1 chunk rowへ分割してevent／outboxと同じacceptance transactionへ含め、2 MB single-row gapを解消する
- outbox repairはD1 compare-and-setの30秒leaseでownerをclaimする。deliveryもD1 delivery token／leaseでattempt ownerをclaimし、terminal duplicateをsinkなしでack、stale transitionをCASで抑止する。lease expiry後のsame event ID再送を許容し、strict exactly-onceは主張しない
- Redis StreamsとCloudflare primitivesのingress、state、queue、idempotency、retry、DLQ、atomicity、duplicate、payload、recovery、observability、operations、reproduction、limitsは[`REDIS_CLOUDFLARE_COMPARISON.md`](REDIS_CLOUDFLARE_COMPARISON.md)で比較する
- credential、Cloudflare account、remote binding、cloud resource、deployment、production trafficは使用していない
- `make doctor`はDocker client／server 29.6.2対pin 29.5.3、Compose 5.3.1対pin 5.1.4の既知driftだけでfailure。pinは変更していない。current Compose、kind E2E、observability、incident runtime、full security scanはこのpost-P0 working treeでは未実行であり、過去のv0.1 evidenceを再検証結果として扱わない

## 未確認事項

- cloud production、実在する外部downstream、multi-node／multi-zone availability、long-running load、本番traffic
- rolling 30日のSLO達成実績
- production Alertmanager、notification destination、on-call運用
- production運用、long-running stability、AWS pending recovery、automatic rollback完了、retry/DLQ remote fault injection、real external downstream、autoscaling、OpenTelemetry/X-Ray、in-flight graceful shutdown、ECR scan severity、外部独立監査

## 配布範囲

Hooklaneはsource-onlyで配布する。source code、Dockerfile、Helm chart、configuration、documentation、検証手順を含む。prebuilt container image、container registry、release artifact、binary distributionは配布しない。application imageはlocal buildし、第三者dependencyとupstream imageには各上流のlicenseとnoticeが適用される。
