# Hooklane limitations

## Intended use

Hooklaneはlocal / portfolio-scale demonstrationとして、HTTP acceptance、Redis Streams queue、at-least-once worker、observability、failure recoveryを再現するreference implementationである。Cloud production environmentは構築しておらず、non-production-readyである。

Short-lived local receiptsは実装contractの検証結果であり、可用性、耐久性、security、performanceを本番条件で証明するものではない。

## Availability and topology

- kindはsingle nodeであり、multi-nodeとmulti-zoneを検証していない。
- Redisはsingle instanceで、Redis HA、replication、automatic failoverを実装していない。
- HelmのRedis PVCは同一cluster内のPod recreationに耐えるだけで、node loss、cluster deletion、backup、restoreを保証しない。
- Workerは既定single replicaである。Pod replacement中の処理継続はpending recoveryに依存する。
- Mock sinkもsingle replicaで、外部downstreamのavailabilityを表さない。
- Autoscaling、capacity-based scaling、multi-cluster traffic routingを実装していない。
- Long-running load test、soak test、chaos testを実施していない。

## Delivery semantics

- Deliveryはat-least-onceであり、exactly-onceではない。
- Downstream side effect後かつRedis ack前のworker停止ではduplicate deliveryが発生し得る。
- Downstreamはevent IDをdeduplication keyとして扱う必要がある。External downstreamでこのcontractを検証していない。
- Retry、backoff、jitter、dead-letterはbounded local policyであり、個別downstreamのrate limitやbusiness retry policyを自動調整しない。
- Dead-letterのlist、replay、approval、bulk remediationを行うoperator interfaceは実装していない。
- Idempotency mapping、event status、stream、dead-letterにTTLやretention rotationを設定していない。

## Production and traffic evidence

- Cloud production deploymentと本番trafficは未検証である。
- External downstreamのnetwork、TLS、rate limit、authentication、partial failureを未検証である。
- Performance targetはsingle-node local environmentのcontract検証用で、capacity planではない。
- [SLO](SLO.md)はrolling 30日の設計targetであり、30日SLO達成実績ではない。
- Local Prometheusの短時間queryやincident drillを30日SLO実績として扱わない。
- Irreversible database migrationはrolling updateとrollback testの対象外である。

## Observability and incident response

- Alertmanager notification destination、paging、on-call rotation、escalation organizationを構築していない。
- Prometheus retentionはlocal検証用の短期間で、long-term metrics storeを持たない。
- Grafana stateはephemeralで、dashboard JSONをGitでprovisionする以外のuser stateを保持しない。
- OpenTelemetry traces、Tempo、distributed tracingはnon-goalである。
- Structured logとevent IDで相関するが、cross-system trace contextを保証しない。
- Incident drillsはproject内mock sink、Redis Pod、worker Podを対象にした安全なlocal injectionだけである。

## Security and networking

- API authentication、authorization、tenant isolation、quota、abuse preventionを実装していない。
- Kubernetes NetworkPolicy、ingress TLS、egress firewallはnon-goalである。
- Composeとkindの公開portはloopbackへ限定するが、shared untrusted hostをsecurity boundaryとして検証していない。
- External secret manager、key rotation、encryption-at-rest、image signing、SBOM attestationを実装していない。
- Single Redisへapplication payloadを保存するため、本番利用前にdata classification、retention、backup、encryptionを設計する必要がある。
- Scanner database更新により将来のfindingは変化し得る。過去のscan passは将来のvulnerability不在を保証しない。

Security controlと残存riskの詳細は[Security](SECURITY.md)を参照する。

## CI and release

- GitHub Actions workflowはlocal static contractを検証済みだが、GitHub hosted Actions上では実行していない。
- Repositoryはsource code、Dockerfile、Helm chart、configuration、documentation、検証手順だけを公開する。
- Prebuilt container image、container registry、release artifact、binary distributionは提供していない。利用者がapplication imageをlocal buildする。
- Runtime dependencyとupstream imageには各上流licenseとnoticeが適用される。Version正本と確認範囲は[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)に記録する。
- Image registry push、release publishing、tag、deployment automationを実装していない。
- Remote repository ownerとpublic reporting channelは確定していない。
- Generated dependency lockとpinned imagesは再現性を高めるが、reproducible-build attestationを提供しない。
- Source-only公開はproduction readinessを意味しない。

## Persistence by environment

| Environment | Persistence scope | Not provided |
|---|---|---|
| Docker Compose | Redisはlocal demo中だけのephemeral state | restart/cleanupを越えるdurability、backup、HA |
| kind / Helm | Single Redis PVCがPod recreationを越えて保持 | node/cluster loss、replication、automatic failover |
| Prometheus / Grafana | Cluster lifetime内のephemeral storage | long-term SLO store、dashboard user-state backup |

## Safe interpretation of evidence

`make verify`、`make e2e-kind`、`make rollout-smoke`、`make observability-smoke`、`make incident-smoke`のpassは、それぞれが定義するlocal contractをその実行時点で満たしたことを示す。次を意味しない。

- Production availabilityまたはsecurity certification
- 30日SLO attainment
- Unlimited throughputやlong-running stability
- Multi-node、multi-zone、external downstream compatibility
- Exactly-once deliveryまたはduplicate side effect不在

実行方法とcleanupは[Operations](OPERATIONS.md)、設計境界は[Architecture](ARCHITECTURE.md)を正本とする。
