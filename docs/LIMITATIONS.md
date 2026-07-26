# Hooklaneの制約

## 対象範囲

Hooklaneは、Webhook受付、Redis Streams queue、at-least-once配送、observability、障害からの回復をローカル構成で確認できるWebhook配送基盤。短時間の検証結果はrepository contractの実証であり、可用性、耐久性、security、performanceを本番条件で証明するものではない。

## 可用性と構成

- kindはsingle-node。multi-nodeとmulti-zoneを検証していない
- Redisはsingle instance。Redis HA、replication、automatic failoverを実装していない
- HelmのRedis PVCは同一cluster内のPod再作成に耐えるだけで、node loss、cluster deletion、backup、restoreを保証しない
- worker、mock sink、Redisは既定でsingle replica
- autoscaling、capacity-based scaling、multi-cluster traffic routingを実装していない
- long-running load test、soak test、chaos testを実施していない

## 配送の性質

- 配送はat-least-onceであり、exactly-onceではない
- downstream side effect後かつRedis ack前のworker停止ではduplicate deliveryが起こり得る
- downstreamはevent IDを重複排除キーとして扱う必要がある。実在する外部downstreamでこのcontractは検証していない
- retry、backoff、jitter、dead-letterはbounded policy。個別downstreamのrate limitやbusiness retry policyを自動調整しない
- dead-letterの一覧、replay、approval、bulk remediationを行うoperator interfaceは実装していない
- idempotency mapping、event status、stream、dead-letterにTTLやretention rotationを設定していない

## 本番とtrafficの実証範囲

- cloud production deploymentと本番trafficは未検証
- 実在する外部downstreamのnetwork、TLS、rate limit、authentication、partial failureは未検証
- performance targetはsingle-nodeのcontract検証用であり、capacity planではない
- [SLO](SLO.md)はrolling 30日の設計目標であり、30日SLO達成実績ではない
- ローカルPrometheusの短時間queryやincident drillを30日SLO実績として扱わない
- irreversible database migrationはrolling updateとrollback testの対象外

## 監視とincident response

- Alertmanager notification destination、paging、on-call rotation、escalation organizationを構築していない
- Prometheus retentionはローカル検証用の短期間。long-term metrics storeを持たない
- Grafana stateはephemeral。dashboard JSONをGitでprovisionする以外のuser stateを保持しない
- OpenTelemetry traces、Tempo、distributed tracingは対象外
- structured logとevent IDで相関するが、cross-system trace contextを保証しない
- incident drillはproject内mock sink、Redis Pod、worker Podを対象にした安全なローカル障害注入だけを扱う

## securityとnetwork

- API authentication、authorization、tenant isolation、quota、abuse preventionを実装していない
- Kubernetes NetworkPolicy、ingress TLS、egress firewallは対象外
- Composeとkindの公開portはloopbackへ限定するが、shared untrusted hostをsecurity boundaryとして検証していない
- remote state bootstrap S3 bucket、ECR artifact stage、foundation stage、runtime stageを明示承認の下で実行した。runtimeではALB target、API task、mock sink taskがhealthyになったが、workerはECS startup health check failureで置換を繰り返したため、delivery、idempotency、retry、dead-letter、pending recovery、graceful shutdown、rollback drillはAWS上で未検証である。実行後はartifact stageへ戻し、bootstrap S3、ECR repository、lifecycle policy、approved image以外のAWS resourceを削除した。AWS Secrets Managerのsecret valueは閲覧していない。secret rotation、Secret lifecycle、worker／Valkey接続の安定性は未実証である。ECR Basic scanはapproved tagへのmanual要求が`UNSUPPORTED`であり、severity結果、image signing、SBOM attestationは未実証である
- Redisにapplication payloadを保存するため、本番導入前にdata classification、retention、backup、encryptionを設計する必要がある
- scanner databaseの更新により将来のfindingは変化し得る。過去のscan passは将来のvulnerability不在を保証しない

security controlと残存riskの詳細は[security](SECURITY.md)を参照する。

## CIと公開

- GitHub hosted Actionsではquality / security / chart gatesとkind delivery and recovery E2Eを実行済み
- GitHub Actionsは現在の公開mainを自動検証するが、cloud productionや本番trafficは検証しない
- v0.1.1のtagがcurrent source baseline。GitHub Releaseの有無はこのsource contractでは主張しない
- source code、Dockerfile、Helm chart、configuration、documentation、検証手順を公開する
- prebuilt container image、container registry、release artifact、binary distributionは配布しない。application imageはlocal buildする
- ECR artifact stageはapply済みで、commit固定tagの3 imageをprivate ECRへpush済みである。foundationとruntime applyは実行後にartifact stageへcleanup済みであり、bootstrap S3、ECR repository、lifecycle policy、approved imageだけを保持する。runtimeのhealthy delivery verificationはworker health failureにより未完了である。release publishingとcloud deployment automationは実行していない
- generated dependency lockとpinned imageは再現性を高めるが、reproducible-build attestationを提供しない

versionと第三者softwareの確認範囲は[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)を参照する。

## 環境ごとの保持範囲

| 環境 | 保持範囲 | 提供しないもの |
|---|---|---|
| Docker Compose | Redisはローカル実行中だけのephemeral state | restart／cleanupを越えるdurability、backup、HA |
| kind / Helm | single Redis PVCがPod再作成を越えて保持 | node／cluster loss、replication、automatic failover |
| Prometheus / Grafana | cluster lifetime内のephemeral storage | long-term SLO store、dashboard user-state backup |

## 検証結果の読み方

`make verify`、`make e2e-kind`、`make rollout-smoke`、`make observability-smoke`、`make incident-smoke`のpassは、それぞれが定義するローカルcontractを実行時点で満たしたことを示す。次を意味しない。

- production availabilityまたはsecurity certification
- 30日SLO attainment
- unlimited throughputやlong-running stability
- multi-node、multi-zone、実在する外部downstreamとの互換性
- exactly-once deliveryまたはduplicate side effect不在

実行手順は[運用](OPERATIONS.md)、設計境界は[アーキテクチャ](ARCHITECTURE.md)、実証済みの事実は[検証根拠](RELEASE_EVIDENCE.md)を正本とする。
