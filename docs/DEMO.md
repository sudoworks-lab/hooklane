# Hooklane 再現手順

## 1. 前提

repository root、Docker daemon、Python、Makeを使う。kind、Helm、kubectl、security scanner、Kubeconformの固定versionは[`toolchain.toml`](../toolchain.toml)を正本とする。secret、credential、external registry、cloud accountは不要。

Hooklaneはsource-onlyで配布し、prebuilt container imageやbinaryは配布しない。Dockerfileからapplication imageをlocal buildし、固定済みupstream imageを取得する。配布範囲は[README](../README.md#配布範囲)を参照する。

```bash
make doctor
```

全required tool、Docker daemon、CPU、memory、diskがpassすることを確認する。host resourceとcache状態により所要時間は変わるため、固定時間は保証しない。

## 2. repository初期化

```bash
bash scripts/init.sh
```

project-local virtual environmentとlock済みdependencyを準備し、config、syntax、doctor、fast smokeを検証する。shell設定、system領域、secret値を変更しない。

## 3. Compose quick demo

```bash
make demo-smoke
```

targetはinit、Compose起動、health、event受付、delivery、status、metricsを検証し、finallyでHooklane Compose projectのcontainer、network、volumeをcleanupする。

段階的に観察する場合は次を使う。

```bash
make compose-up
make smoke
make e2e-local
make compose-down
```

## 4. event受付

`make smoke`または`make demo-smoke`がvalid eventを投入し、APIの`202 Accepted`とUUID event IDを検査する。request payloadと`Idempotency-Key`生値はoutputへ表示しない。Redisへeventと初期statusを永続化できない場合は202を返さない。

manual request bodyを文書へ複製せず、正本runnerの[`local_e2e.py`](../scripts/local_e2e.py)を使う。

## 5. delivered status

runnerは返されたevent IDを`GET /v1/events/{event_id}`でpollし、statusが`delivered`、attempt countが1、response event IDが一致することを確認する。status responseにpayload本文は含まれない。

Composeを保持している場合、既知event IDは次の形で確認する。

```bash
curl --fail http://127.0.0.1:18080/v1/events/<event-id>
```

## 6. idempotency

```bash
make e2e-local
```

同じ`Idempotency-Key`と同じcontentが同じevent IDを返し、異なるcontentへのkey再利用が409になることを検査する。これはAPI受付の重複排除であり、downstream deliveryのexactly-once保証ではない。

## 7. metricsとdashboard

Compose metrics endpointは次で確認する。

```bash
make compose-up
curl --fail http://127.0.0.1:18080/metrics
make compose-down
```

observability構成は次で確認する。

```bash
make observability-up
make observability-smoke
make observability-down
```

application targetがUP、`hooklane_http_requests_total`とdelivery metricsが増加し、`hooklane_queue_depth`と`hooklane_pending_messages`が最終的に0へ戻り、Grafana dashboard provisioning、PromQL parse、alert recoveryがsuccessとなることを確認する。

## 8. kind deploy

```bash
make cluster-up
make deploy
make chart-smoke
make cluster-down
```

API 2 replica、worker、mock sink、RedisがReadyとなり、Helm testで202受付と配送がsuccessとなることを確認する。local image buildとkind loadを使い、external registryへpushしない。

## 9. rolling update

```bash
make cluster-up
make deploy
make rollout-smoke
make cluster-down
```

APIの`maxUnavailable: 0`、`maxSurge: 1`、継続request、Ready endpoint維持、worker graceful drain、in-flight event loss 0を確認する。

## 10. bad releaseとrollback

`make rollout-smoke`はreadinessを通らない安全なbad releaseをlocal cluster内に作り、failureを検知する。その後Helm rollbackでnormal revisionへ戻し、全workload Ready、正常配送、release history、failure injection残存なしを確認する。irreversible database migrationは対象外。

## 11. incident drill

```bash
make incident-smoke
```

次のdrillを順に実行する。

1. downstream 5xxでdelivery failure、retry、backlog、alert、復旧を確認する
2. Redis outageでreadiness false、liveness維持、non-202、Redis metric／alert、PVC state維持、復旧を確認する
3. worker stopでside effect後・ack前のpending、replacement claim、同じevent IDのretry、duplicate可能性、accepted event loss 0を確認する

incident記録、Runbook、postmortemは[運用](OPERATIONS.md#incidentとpostmortemの一覧)を参照する。

## 12. cleanup

実行したflowに対応するtargetだけを使う。

```bash
make compose-down
make observability-down
make cluster-down
make runtime-hygiene-check
```

`runtime-hygiene-check`はHooklane専用kind cluster、Compose container／network／volume、dedicated kubeconfig、test Redis containerが残っていないことを確認する。無関係なDocker resourceへ触れない。

## 13. 確認内容

### アーキテクチャ

- API、Redis Streams、worker、mock sinkの境界が[アーキテクチャ](ARCHITECTURE.md)とruntime workloadに一致する
- accepted eventはRedis queue／statusへ保存され、workerがevent IDを維持して配送する
- Composeとkind／Helmのローカル構成を同じapplication imageで検証する

### 障害時の動作

- downstream 5xxはretry／backlog、Redis outageはreadiness／fail-closed、worker stopはpending recoveryとして観測される
- bad releaseはReadyにならず、rollback後に正常deliveryが回復する
- incident終了時にfailure injection、queue、pending、active alertが通常状態へ戻る

### healthの意味

- livenessはprocess health、readinessはtraffic eligibility、metricsは観測dataとして分離する
- Redis outage中もAPI livenessは維持され、readinessはfalse、新規eventは202成功扱いにならない
- shutdown開始Podはreadinessから外れ、workerはbounded graceful drainを行う

### 検証結果

- `make verify`がlint、strict type、unit／integration、security、chart、docsをfail-closedで集約する
- `make demo-smoke`、`make e2e-kind`、`make rollout-smoke`、`make observability-smoke`、`make incident-smoke`が各runtime contractを機械判定する
- `make clean-room`がtracked candidateだけから同じflowを再実行し、cleanupとrepository hygieneを確認する
- GitHub hosted Actionsではquality / security / chart gatesとkind delivery and recovery E2Eを確認済み

### 制約

- ローカル、single-node kind、single Redis、default single worker、project mock sinkの短時間検証
- deliveryはat-least-onceでduplicate可能性があり、downstreamのevent ID重複排除が必要
- cloud production、実在する外部downstream、long-running load、30日SLO attainmentは未確認
- Hooklane sourceはMIT Licenseで提供し、third-party dependencyとupstream imageには各上流licenseとnoticeが適用される。確認範囲は[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)を参照する

## 14. 既知の制約

一覧は[制約](LIMITATIONS.md)を参照する。再現手順の結果をproduction readiness、exactly-once delivery、HA、capacity、security certification、30日SLO実績として解釈しない。
