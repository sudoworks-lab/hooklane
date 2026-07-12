# Hooklane architecture

## System context

Hooklaneは、HTTPで受け付けたeventをRedis Streamsへ保存し、非同期workerが固定されたdownstreamへ配送するlocal demonstrationである。信頼されないclient inputをAPI boundaryで検証し、受付と配送を分離する。cloud production、外部downstream、multi-zone availabilityを実装済みとは扱わない。制約は[Limitations](LIMITATIONS.md)を正本とする。

```text
client
  | POST /v1/events
  v
API -- Redis Streams + status/retry/dead-letter -- worker -- mock sink
 |                  ^                              |
 +-- status API ----+                              +-- retry / pending recovery

API / worker / mock sink -- /metrics --> Prometheus --> Grafana / alert rules
```

## Component responsibilities

| Component | Responsibility | Does not own |
|---|---|---|
| API | request validation、idempotency判定、atomic enqueue、status参照、health、metrics | downstream delivery、retry execution |
| Redis Streams | event queue、consumer-group pending、event status、retry schedule、dead-letterのlocal state | HA、automatic failover、business-level deduplication |
| worker | stream consumption、delivery、retry/dead-letter判定、pending claim、graceful drain | HTTP受付、exactly-once保証 |
| mock sink | 正常配送のreceiptと安全なfailure injection | 実在する外部serviceの再現、本番耐久性 |
| Prometheus / Grafana | application metricsのscrape、SLI dashboard、local alert評価 | 長期SLO実績、通知routing、on-call |

実装入口は[`src/hooklane`](../src/hooklane)、local topologyは[`compose.yaml`](../compose.yaml)、Kubernetes topologyは[`charts/hooklane`](../charts/hooklane/Chart.yaml)にある。

## Request-to-delivery data flow

1. Clientは`POST /v1/events`へJSON requestと`Idempotency-Key`を送る。
2. APIはschemaとidempotency contractを検証する。有効な新規requestにはUUID event IDを割り当てる。
3. APIはevent、初期status、idempotency mappingをRedis上で原子的に作成する。永続化できた場合だけ`202 Accepted`を返す。
4. Workerはconsumer groupからeventを取得し、statusを`delivering`へ遷移させ、event IDをdownstream receipt keyとしてmock sinkへ配送する。
5. 成功時はstatusを`delivered`へ更新してstream messageをackする。retryable failureはretry scheduleへ移し、policy上限またはnon-retryable failureは`dead_letter`へ終端する。
6. `GET /v1/events/{event_id}`はRedis上のstatus、attempt count、timestampsを返す。payload本文はstatus responseへ再掲しない。

Enqueueの正本実装は[`src/hooklane/queue/events.py`](../src/hooklane/queue/events.py)、配送policyは[`src/hooklane/worker/service.py`](../src/hooklane/worker/service.py)と[`src/hooklane/delivery/sink.py`](../src/hooklane/delivery/sink.py)にある。

## Event status lifecycle

通常のlifecycleは`queued`から`delivering`を経て`delivered`へ進む。retryable failureでは`retry_scheduled`から再び`queued`へ戻り、最終的に`delivered`または`dead_letter`となる。worker停止時はmessageがRedis consumer-groupのpendingに残り、replacement workerがidle threshold後に同じevent IDをclaimする。

Status transitionとstream acknowledgementは、accepted eventをworker process終了だけで失わないように設計する。ただしRedis自体のHAは提供しない。

## Idempotency contract

同じ`Idempotency-Key`と同じrequest contentを再送すると、APIは既存event IDを返し、新しいeventをenqueueしない。同じkeyを異なるcontentへ再利用すると`409 Conflict`を返す。keyの生値はlog、metric label、status responseへ出さず、Redis mappingにはdigestを用いる。

IdempotencyはAPI受付の重複抑止であり、worker deliveryのexactly-once保証ではない。Clientはkeyをrequest単位で安定して再利用し、異なるrequest間で使い回さない。

## Delivery guarantee, retry, and dead-letter

配送保証はat-least-onceである。Workerがdownstream side effect後かつRedis ack前に停止すると、同じevent IDを再配送し得る。Downstreamはevent IDをdeduplication keyとして扱い、同じIDのside effectを一度だけ適用する責務を持つ。この判断は[ADR 0001](adr/0001-redis-streams-at-least-once.md)に記録する。

Timeout、connection failure、HTTP 429、HTTP 5xxはretryableであり、bounded exponential backoffとjitterを使う。HTTP 4xxはnon-retryableである。Retry policyの上限を超えたeventはdead-letterへ移る。Arbitrary exception messageは外部responseやstructured logへ流さず、有限集合の`reason_code`へ分類する。

## Pending message recovery

Workerはconsumer-groupのpending messageを観測し、idle thresholdを超えたmessageをclaimする。Claim後もevent IDは変えず、attempt countとstatus transitionを更新する。実際のworker停止、duplicate可能性、accepted event loss 0の検証receiptは[worker-stop incident](incidents/worker-stop.md)と[blameless postmortem](incidents/postmortem-worker-stop.md)にある。

## Health semantics and graceful shutdown

Health endpointとapplication metrics endpointは分離する。

- Livenessはprocessが応答可能かを表し、Redis outageだけではfalseにしない。
- Readinessは新規trafficを安全に処理できるかを表し、Redis接続不能またはshutdown開始時にfalseとなる。
- Metricsは`/metrics`で公開し、health responseへ混在させない。
- APIはshutdown開始後に新規enqueueを受け付けず、workerはin-flight処理をbounded drainした後に終了する。

Kubernetesはreadinessに成功したAPI PodだけをService endpointへ加える。詳細な判断は[ADR 0002](adr/0002-health-semantics.md)、Redis outageの実測は[incident record](incidents/redis-outage.md)を参照する。

## Deployment topologies

### Docker Compose

[`compose.yaml`](../compose.yaml)はAPI、worker、mock sink、Redisを同一projectに作成する。Host公開portはloopbackへ限定する。Containerはnon-root、read-only root filesystem、dropped capabilitiesを基本とする。Compose Redisは一時領域を使い、`make compose-down`でproject volumeを除去するため、長期保存先ではない。

### kind and Helm

[`deploy/kind/cluster.yaml`](../deploy/kind/cluster.yaml)はproject専用のsingle-node kind clusterを定義し、[`charts/hooklane`](../charts/hooklane/Chart.yaml)がworkload、Service、probe、resource、PDB、rolling strategy、Redis PVCを管理する。APIの既定replicaは2、worker、mock sink、Redisは各1である。Redis PVCはPod再作成に耐えるが、cluster deletion、node loss、Redis failureへのHAを提供しない。

Local image buildとkind loadだけを使い、external registryへpushしない。検証境界は[ADR 0003](adr/0003-local-kind-observability.md)に記録する。

## Observability

API、worker、mock sinkは共通contractの1行JSON structured logを出力する。Correlationにはrequest IDまたはevent IDを使うが、payload、`Idempotency-Key`生値、credential、Redis connection情報、cookie、stack traceを記録しない。

Application metricsは`hooklane_` prefixと有限label集合を使う。event ID、request ID、raw URL、user input、exception messageをlabelへ入れない。Prometheusはannotated Podを通常のscrape configで探索し、Prometheus OperatorとServiceMonitorは採用しない。Grafana dashboard、SLI PromQL、alert rulesの正本は次の通りである。

- [Dashboard JSON](../charts/hooklane/files/grafana/dashboards/hooklane-overview.json)
- [SLI PromQL mapping](../observability/sli-promql.json)
- [Alert rules](../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- [SLI / SLO design targets](SLO.md)

## CI structure

[`ci.yml`](../.github/workflows/ci.yml)には`quality`と`e2e-kind`の2 jobがある。Quality jobはlocalの`make verify`を呼び、kind jobはその成功後にlocalの`make e2e-kind`を呼ぶ。Workflowはread-only repository permission、full commit SHAで固定したactions、secretを必要としないpull request triggerを使う。Hosted GitHub Actions上の実行は未確認である。

## Trust boundaries

主なboundaryはclientからAPI、workerからdownstream、PodからRedis、Prometheusからapplication metricsである。

- Client inputは信頼せずAPI schemaで検証する。Authenticationとmulti-tenant authorizationは実装しないため、既定のlocal loopbackまたはcluster内Service boundaryを越えて公開しない。
- Downstream destinationはapplication設定の固定allowlistを使い、request由来のarbitrary URLへ配送しない。
- Redis connection情報はSecretから注入できるが、log、metric、diagnosticsへ出さない。
- PrometheusのServiceAccount tokenはautomatic mountを無効にし、namespace内Pod discoveryに必要なshort-lived projected tokenとread-only Roleだけを与える。
- Grafanaのanonymous Viewerはcluster-localだけに限定し、外部公開portを持たない。

Security boundaryと例外は[Security](SECURITY.md)、machine-readable policyは[`container-policy.json`](../container-policy.json)と[`security-policy.json`](../security-policy.json)を参照する。

## Data retention and persistence

Applicationはevent stream、status、retry schedule、dead-letter、idempotency mappingにTTLを設定しない。Helm topologyではsingle Redis PVCが同一cluster内のPod recreationを越えて状態を保持するが、backup、restore、retention rotation、cross-node replicationはない。Compose topologyはRedisを一時利用する。Prometheusはlocal短期retention、Grafanaはephemeral storageであり、cluster cleanup後に観測履歴を保持しない。

Payload minimizationとretention policyは本番導入前に別途設計が必要である。

## Failure modes

| Failure mode | Expected behavior | Detection and recovery evidence |
|---|---|---|
| Downstream 5xx | retry schedule、backlog、最終成功またはdead-letter | [Delivery Runbook](runbooks/HooklaneDeliveryFailureRateHigh.md)、[incident](incidents/downstream-5xx.md) |
| Redis outage | API not ready、enqueue非202、worker停止、復旧後再開 | [Redis Runbook](runbooks/HooklaneRedisOperationFailures.md)、[incident](incidents/redis-outage.md) |
| Worker stop after side effect | pending claim、同じevent IDを再配送し得る | [Queue Runbook](runbooks/HooklaneQueueBacklogGrowing.md)、[incident](incidents/worker-stop.md) |
| Bad API release | old Ready Podを維持し、failure検知後Helm rollback | [Operations](OPERATIONS.md#rolling-update-and-rollback) |
| Single Redis/node loss | availabilityまたはstate lossの可能性 | [Limitations](LIMITATIONS.md) |

## Non-goals

Exactly-once delivery、external downstream certification、multi-regionまたはmulti-zone deployment、Redis HA、autoscaling、distributed tracing、OpenTelemetry trace collection、Alertmanager notification routing、irreversible database migrationはこの実装のnon-goalである。Network isolationやproduction authenticationを含む本番hardeningも完了済みとは扱わない。

## Design decisions and operational evidence

- [ADR 0001: Redis Streams and at-least-once delivery](adr/0001-redis-streams-at-least-once.md)
- [ADR 0002: Health semantics](adr/0002-health-semantics.md)
- [ADR 0003: Local kind and observability boundary](adr/0003-local-kind-observability.md)
- [Operations](OPERATIONS.md)
- [Security](SECURITY.md)
- [Runbook index](OPERATIONS.md#alert-and-runbook-index)
- [Incident and postmortem index](OPERATIONS.md#incident-and-postmortem-index)
