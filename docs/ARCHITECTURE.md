# Hooklane アーキテクチャ

## システム概要

Hooklaneは、HTTPで受け付けたeventをRedis Streamsへ保存し、非同期workerが固定されたdownstreamへ配送するWebhook配送基盤。clientからの入力をAPI boundaryで検証し、受付と配送を分離する。実証範囲と未確認事項は[制約](LIMITATIONS.md)を正本とする。

```text
client
  | POST /v1/events
  v
API -- Redis Streams + status/retry/dead-letter -- worker -- mock sink
 |                  ^                              |
 +-- status API ----+                              +-- retry / pending recovery

API / worker / mock sink -- /metrics --> Prometheus --> Grafana / alert rules
```

## コンポーネントの責務

| コンポーネント | 担当すること | 担当しないこと |
|---|---|---|
| API | request validation、idempotency判定、atomic enqueue、status参照、health、metrics | downstream配送、retry実行 |
| Redis Streams | event queue、consumer-group pending、event status、retry schedule、dead-letterの状態保持 | HA、automatic failover、business-level deduplication |
| worker | stream消費、配送、retry／dead-letter判定、pending claim、graceful drain | HTTP受付、exactly-once保証 |
| mock sink | 配送確認と安全な障害注入 | 実在する外部serviceの再現 |
| Prometheus / Grafana | application metricsのscrape、SLI dashboard、alert評価 | 長期SLO実績、通知routing、on-call |

実装は[`src/hooklane`](../src/hooklane)、Docker Compose構成は[`compose.yaml`](../compose.yaml)、Kubernetes構成は[`charts/hooklane`](../charts/hooklane/Chart.yaml)にある。

## 受付から配送までの流れ

1. clientが`POST /v1/events`へJSON requestと`Idempotency-Key`を送る
2. APIがschemaとidempotency contractを検証し、新規requestへUUID event IDを割り当てる
3. APIがevent、初期status、idempotency mappingをRedis上で原子的に作成する。永続化できた場合だけ`202 Accepted`を返す
4. workerがconsumer groupからeventを取得し、statusを`delivering`へ遷移させ、event IDをreceipt keyとしてmock sinkへ配送する
5. 成功時はstatusを`delivered`へ更新してstream messageをackする。retryable failureはretry scheduleへ移し、policy上限またはnon-retryable failureは`dead_letter`へ終端する
6. `GET /v1/events/{event_id}`はstatus、attempt count、timestampを返す。payload本文はresponseへ再掲しない

enqueueの実装は[`queue/events.py`](../src/hooklane/queue/events.py)、配送policyは[`worker/service.py`](../src/hooklane/worker/service.py)と[`delivery/sink.py`](../src/hooklane/delivery/sink.py)を正本とする。

## event statusの遷移

通常は`queued`から`delivering`を経て`delivered`へ進む。retryable failureでは`retry_scheduled`から`queued`へ戻り、最終的に`delivered`または`dead_letter`となる。worker停止時はmessageがconsumer groupのpendingに残り、別workerがidle threshold後に同じevent IDをclaimする。

status transitionとstream acknowledgementは、worker processの停止だけでaccepted eventを失わないように設計する。Redis自体のHAは提供しない。

## idempotency contract

同じ`Idempotency-Key`と同じrequest contentを再送すると、APIは既存event IDを返し、新しいeventをenqueueしない。同じkeyを異なるcontentへ再利用すると`409 Conflict`を返す。keyの生値はlog、metric label、status responseへ出さず、Redis mappingにはdigestを使う。

idempotencyはAPI受付の重複抑止であり、worker配送のexactly-once保証ではない。clientはkeyをrequest単位で安定して再利用する。

## 配送保証、retry、dead-letter

配送保証はat-least-once。workerがdownstream side effect後かつRedis ack前に停止すると、同じevent IDを再配送し得る。downstreamはevent IDを重複排除キーとして扱い、同じIDのside effectを一度だけ適用する責務を持つ。この判断は[ADR 0001](adr/0001-redis-streams-at-least-once.md)に記録する。

timeout、connection failure、HTTP 429、HTTP 5xxはretryableとし、bounded exponential backoffとjitterを使う。HTTP 4xxはnon-retryable。retry policyの上限を超えたeventはdead-letterへ移す。arbitrary exception messageは外部responseやstructured logへ流さず、有限集合の`reason_code`へ分類する。

## pending message回収

workerはconsumer-groupのpending messageを観測し、idle thresholdを超えたmessageをclaimする。claim後もevent IDは変えず、attempt countとstatus transitionを更新する。worker停止時の検証根拠は[worker stopの記録](incidents/worker-stop.md)と[postmortem](incidents/postmortem-worker-stop.md)を参照する。

## healthとgraceful shutdown

health endpointとapplication metrics endpointは分離する。

- livenessはprocessが応答可能かを表し、Redis outageだけではfalseにしない
- readinessは新規trafficを安全に処理できるかを表し、Redis接続不能またはshutdown開始時にfalseとなる
- metricsは`/metrics`で公開し、health responseへ混在させない
- APIはshutdown開始後に新規enqueueを受け付けず、workerはin-flight処理をbounded drainして終了する

Kubernetesはreadinessに成功したAPI PodだけをService endpointへ加える。詳細な判断は[ADR 0002](adr/0002-health-semantics.md)、Redis outageの確認は[incident記録](incidents/redis-outage.md)を参照する。

## deployment構成

### Docker Compose

[`compose.yaml`](../compose.yaml)はAPI、worker、mock sink、Redisを同一projectに作成する。host公開portはloopbackへ限定する。containerはnon-root、read-only root filesystem、dropped capabilitiesを基本とする。Compose Redisは一時利用であり、`make compose-down`がproject volumeを除去する。

### kindとHelm

[`deploy/kind/cluster.yaml`](../deploy/kind/cluster.yaml)はproject専用single-node kind clusterを定義する。[`charts/hooklane`](../charts/hooklane/Chart.yaml)はworkload、Service、probe、resource、PDB、rolling strategy、Redis PVCを管理する。APIの既定replicaは2、worker、mock sink、Redisは各1。

local image buildとkind loadだけを使い、external registryへpushしない。検証境界は[ADR 0003](adr/0003-local-kind-observability.md)に記録する。

## observability

API、worker、mock sinkは共通contractのJSON structured logを出力する。correlationにはrequest IDまたはevent IDを使うが、payload、`Idempotency-Key`生値、credential、Redis connection情報、cookie、stack traceを記録しない。

application metricsは`hooklane_` prefixと有限label集合を使う。event ID、request ID、raw URL、user input、exception messageをlabelへ入れない。Prometheusはannotated Podを通常のscrape configで探索する。

- [Grafana dashboard](../charts/hooklane/files/grafana/dashboards/hooklane-overview.json)
- [SLI PromQL対応表](../observability/sli-promql.json)
- [alert rule](../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- [SLI / SLO設計目標](SLO.md)

## GitHub Actions構成

[`ci.yml`](../.github/workflows/ci.yml)には`quality`と`e2e-kind`の2 jobがある。quality jobは`make verify`を呼び、kind jobはそのsuccess後に`make e2e-kind`を呼ぶ。workflowはread-only repository permission、full commit SHAで固定したaction、secretを必要としないpull request triggerを使う。

GitHub hosted Actionsではquality / security / chart gatesとkind delivery and recovery E2Eを実行済み。Hosted CIは公開mainの自動検証であり、cloud productionや本番trafficの実績ではない。

## trust boundary

主なboundaryはclientからAPI、workerからdownstream、PodからRedis、Prometheusからapplication metrics。

- client inputは信頼せずAPI schemaで検証する。authenticationとtenant authorizationは実装しない
- downstream destinationは固定allowlistを使い、request由来のarbitrary URLへ配送しない
- Redis connection情報はSecretから注入できるが、log、metric、diagnosticsへ出さない
- PrometheusのServiceAccount tokenはautomatic mountを無効にし、namespace内Pod discoveryに必要なshort-lived projected tokenとread-only Roleだけを与える
- Grafanaのanonymous Viewerはcluster-localに限定し、外部公開portを持たない

security boundaryと例外は[security](SECURITY.md)、machine-readable policyは[`container-policy.json`](../container-policy.json)と[`security-policy.json`](../security-policy.json)を参照する。

## data retentionと永続性

applicationはevent stream、status、retry schedule、dead-letter、idempotency mappingにTTLを設定しない。Helm構成ではsingle Redis PVCが同一cluster内のPod再作成を越えて状態を保持するが、backup、restore、retention rotation、cross-node replicationはない。Compose構成はRedisを一時利用する。Prometheusは短期retention、Grafanaはephemeral storageであり、cluster cleanup後に観測履歴を保持しない。

payload minimizationとretention policyは本番導入前に別途設計が必要となる。

## failure mode

| failure mode | 期待する動作 | 検知と復旧の根拠 |
|---|---|---|
| downstream 5xx | retry schedule、backlog、最終成功またはdead-letter | [Delivery Runbook](runbooks/HooklaneDeliveryFailureRateHigh.md)、[incident](incidents/downstream-5xx.md) |
| Redis outage | API not ready、enqueue非202、worker停止、復旧後再開 | [Redis Runbook](runbooks/HooklaneRedisOperationFailures.md)、[incident](incidents/redis-outage.md) |
| worker stop after side effect | pending claim、同じevent IDを再配送し得る | [Queue Runbook](runbooks/HooklaneQueueBacklogGrowing.md)、[incident](incidents/worker-stop.md) |
| bad API release | old Ready Podを維持し、failure検知後にHelm rollback | [運用](OPERATIONS.md#rolling-updateとrollback) |
| single Redis／node loss | availabilityまたはstate lossの可能性 | [制約](LIMITATIONS.md) |

## 対象外

exactly-once delivery、実在する外部downstreamとの互換性認定、multi-regionまたはmulti-zone deployment、Redis HA、autoscaling、distributed tracing、OpenTelemetry trace collection、Alertmanager notification routing、irreversible database migrationは対象外。詳細は[制約](LIMITATIONS.md)を参照する。
