# Hooklane reproducible demonstration

## 1. Prerequisites

Repository root、Docker daemon、Python、Makeを使用する。kind、Helm、kubectl、security scanner、Kubeconformの固定versionは[`toolchain.toml`](../toolchain.toml)を正本とする。Secret、credential、external registry、cloud accountは不要である。

このrepositoryはsource-onlyであり、prebuilt container imageやbinaryを配布しない。DemoではDockerfileからapplication imageをlocal buildし、pinned upstream imageを取得する。

Resourceとtoolを確認する。

```bash
make doctor
```

Expected resultは全required tool、Docker daemon、CPU、memory、diskがpassすることである。Host resourceとcache状態により所要時間は変わるため、固定時間は保証しない。

## 2. Repository initialization

```bash
bash scripts/init.sh
```

Project-local virtual environmentとlock済みdependencyを準備し、config、syntax、doctor、fast smokeを検証する。Shell設定、system領域、secret値を変更しない。

## 3. Compose quick demo

最短のself-cleaning flowは次である。

```bash
make demo-smoke
```

Targetはinit、Compose起動、health、event受付、delivery、status、metricsを検証し、finallyでHooklane Compose projectのcontainer、network、volumeをcleanupする。

段階的に観察する場合は次を使う。

```bash
make compose-up
make smoke
make e2e-local
```

確認後は必ずcleanupする。

```bash
make compose-down
```

## 4. Event acceptance

`make smoke`または`make demo-smoke`がvalid eventを投入し、APIの`202 Accepted`とUUID event IDを検査する。Request payloadと`Idempotency-Key`生値はoutputへ表示しない。Redisへeventと初期statusを永続化できない場合は202を返さない。

Manual request bodyを文書へ複製せず、正本runner[`local_e2e.py`](../scripts/local_e2e.py)を使う。

## 5. Delivered status

Runnerは返されたevent IDを`GET /v1/events/{event_id}`でpollし、statusが`delivered`、attempt countが1、response event IDが一致することを確認する。Status responseにpayload本文は含まれない。

Composeを保持している場合、既知event IDは次の形で確認できる。

```bash
curl --fail http://127.0.0.1:18080/v1/events/<event-id>
```

## 6. Idempotency

```bash
make e2e-local
```

同じ`Idempotency-Key`と同じcontentが同じevent IDを返し、異なるcontentへのkey再利用が409になることを検査する。これはAPI受付のdeduplicationであり、downstream deliveryのexactly-once保証ではない。

## 7. Metrics and dashboard

Compose metrics endpointは次で確認する。

```bash
make compose-up
curl --fail http://127.0.0.1:18080/metrics
make compose-down
```

Full observability flowは次である。

```bash
make observability-up
make observability-smoke
make observability-down
```

Expected resultはapplication target UP、`hooklane_http_requests_total`とdelivery metricsの増加、`hooklane_queue_depth`と`hooklane_pending_messages`の最終0、Grafana dashboard provisioning、PromQL parse、alert recoveryである。

## 8. kind deploy

Application chartの段階確認は次の通りである。

```bash
make cluster-up
make deploy
make chart-smoke
```

Expected resultはAPI 2 replica、worker、mock sink、RedisがReadyとなり、Helm testで202受付と配送が成功することである。Local image buildとkind loadを使用し、external registryへpushしない。

終了時は次を実行する。

```bash
make cluster-down
```

## 9. Rolling update

```bash
make cluster-up
make deploy
make rollout-smoke
make cluster-down
```

API `maxUnavailable: 0`、`maxSurge: 1`、継続request、Ready endpoint維持、worker graceful drain、in-flight event loss 0を検証する。

## 10. Bad release and rollback

`make rollout-smoke`はreadinessを通らない安全なbad releaseをlocal cluster内に作り、false successにせずfailureを検知する。その後Helm rollbackでnormal revisionへ戻し、全workload Ready、正常配送、release history、failure injection残存なしを確認する。Irreversible database migrationは対象外である。

## 11. Incident drills

```bash
make incident-smoke
```

次のdrillをID順に実行する。

1. Downstream 5xxでdelivery failure、retry、backlog、alert、復旧を確認する。
2. Redis outageでreadiness false、liveness維持、non-202、Redis metric/alert、PVC state維持、復旧を確認する。
3. Worker stopでside effect後・ack前のpending、replacement claim、same event ID retry、duplicate可能性、accepted event loss 0を確認する。

Incident receipt、Runbook、postmortemは[Operations index](OPERATIONS.md#incident-and-postmortem-index)を参照する。

## 12. Cleanup

実行したflowに対応するtargetだけを使用する。

```bash
make compose-down
make observability-down
make cluster-down
make runtime-hygiene-check
```

`runtime-hygiene-check`はHooklane専用kind cluster、Compose container/network/volume、dedicated kubeconfig、test Redis containerが残っていないことを確認する。Unrelated Docker resourceへ触れない。

## 13. Expected evidence

### Architecture

- API、Redis Streams、worker、mock sinkの境界が[Architecture](ARCHITECTURE.md)とruntime workloadに一致する。
- Accepted eventはRedis queue/statusへ保存され、workerがevent IDを維持して配送する。
- Composeとkind/Helmの二つのlocal topologyを同じapplication imageで検証する。

### Failure modes

- Downstream 5xxはretry/backlog、Redis outageはreadiness/fail-closed、worker stopはpending recoveryとして観測される。
- Bad releaseはReadyにならず、rollback後に正常deliveryが回復する。
- Incident終了時にfailure injection、queue、pending、active alertが正常化する。

### Health semantics

- Livenessはprocess health、readinessはtraffic eligibility、metricsは観測dataとして分離される。
- Redis outage中もAPI livenessは維持され、readinessはfalse、新規eventは202成功扱いにならない。
- Shutdown開始Podはreadinessから外れ、workerはbounded graceful drainを行う。

### Verification results

- `make verify`がlint、strict type、unit/integration、security、chart、docsをfail-closedで集約する。
- `make demo-smoke`、`make e2e-kind`、`make rollout-smoke`、`make observability-smoke`、`make incident-smoke`が各runtime contractを機械判定する。
- `make clean-room`がtracked HEADまたは明示stage済みcandidateだけから同じflowを再実行し、cleanupとrepository hygieneを確認する。

### Constraints

- Local、single-node kind、single Redis、default single worker、project mock sinkの短時間検証である。
- Deliveryはat-least-onceでduplicate可能性があり、downstream event-ID deduplicationが必要である。
- GitHub hosted Actions、cloud production、external downstream、long-running load、30日SLO attainmentは未確認である。
- Hooklane sourceはMIT Licenseで提供し、third-party dependencyとupstream imageには各上流licenseとnoticeが適用される。Exact referenceは[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)を参照する。
- Source-only公開はproduction readiness、hosted service、binary distributionを意味しない。

## 14. Known limitations

Full listは[Limitations](LIMITATIONS.md)を参照する。Demo evidenceをproduction readiness、exactly-once delivery、HA、capacity、security certification、30日SLO実績として解釈しない。
