# Redis outage incident drill

- Command: `make incident-redis-outage`
- Alert: [`HooklaneRedisOperationFailures`](../runbooks/HooklaneRedisOperationFailures.md)、[`HooklaneApiHighErrorRate`](../runbooks/HooklaneApiHighErrorRate.md)
- SLI/SLO: [`API受付可用性`](../SLO.md#api受付可用性)
- Dashboard: `Hooklane SLI and Operations` の `Redis error`、`API acceptance success rate`、`Queue depth`、`Available API replicas`

## 再現手順

専用kind clusterのHooklane Redis StatefulSetだけを一時的にreplica 0へscaleする。APIとworkerは起動したまま、各API Podのliveness/readinessと直接POST、workerのRedis signalを確認する。PVCを削除せず同じStatefulSetをreplica 1へ戻す。

## 期待する影響

API readinessとworker readinessはfalseになるが、livenessとprocessは維持される。有効な新規eventはRedisへ永続化できないため202にせず、固定503またはService endpoint不在としてfail closedする。既存PVC上のaccepted eventは保持される。

## 期待するmetrics

- `hooklane_redis_operation_failures_total`がAPIとworkerで増加する。
- `hooklane_enqueue_total{outcome="failure",reason_code="storage_unavailable"}`が増加する。
- `hooklane_service_ready{service="api"}`とworker readiness signalが0になる。
- 復旧後は30秒の`increase(hooklane_redis_operation_failures_total[30s])`が0となり、queue depthとpendingが0へ戻る。

## 期待するalert

`HooklaneRedisOperationFailures`がfiringになる。APIへ直接行ったfailure requestにより`HooklaneApiHighErrorRate`も補助signalになり得る。alert annotationからRedis Runbook、dashboard、API受付可用性SLOへ辿る。

## Structured log

APIとworkerの`redis_operation_failed`を`service`と`reason_code=redis_error`で確認する。APIの受付失敗は`request_rejected`と`storage_unavailable`で確認する。Redis URL、payload、credential、secret-like value、arbitrary exception messageは収集しない。

## 初動切り分け

[`HooklaneRedisOperationFailures` Runbook](../runbooks/HooklaneRedisOperationFailures.md)に従い、Redis StatefulSet/Pod/PVC、API readinessとliveness、worker readiness、operation別Redis failureを確認する。downstream failureやworker停止と分類を分ける。

## 暫定対応

今回所有するStatefulSetを既存PVCのままreplica 1へ戻し、Readyを待つ。PVC削除、key削除、database flush、AOF修復、credential閲覧は行わない。復旧まで新規受付を成功扱いしない。

## 復旧手順

`hooklane-redis` StatefulSetをreplica 1へscaleし、`hooklane-redis-0`のReadyとPVC UID不変を確認する。API readinessとworker readinessの回復後に新規eventを投入する。

## 復旧確認

全API Podのreadiness/liveness、worker Ready、既存event status、stream/status件数、Redis alert inactive、新規eventのattempt 1 delivery、queue depth/pending 0を確認する。

## データ消失の有無

停止前後のRedis stream長、event status件数、PVC UID、baseline eventのstatusとsink receiptを比較する。停止中の失敗POSTでstream/status件数が増えず、復旧後もbaseline eventが同じevent IDでdeliveredなら、確認可能なdata lossとpartial enqueueは0件と判定する。

## 再発防止候補

Redis HA、backup/restore、capacity planning、operation別alert、dependency outage test、受付側circuit breakerを候補とする。Redis Cluster/Sentinel/managed Redisは現行P0のNon-goalであり、このdrillでは追加しない。

## 制約と未確認事項

単一node kind、単一Redis、短時間のgraceful StatefulSet停止だけを検証する。node/PV loss、AOF corruption、replication、automatic failover、長時間停止、本番通知経路は未確認である。

## 検証receipt

2026-07-12T09:44:35+09:00に`make incident-redis-outage`を実行した。Redis停止中はAPI 2 PodとworkerがNotReady、API livenessは200、restart countは全Pod 0だった。直接POSTは503かつevent IDなしで、API/worker Redis failure metricは15/10、failed enqueueは1、Redis alertはfiring、両serviceのstructured logは`redis_error`だった。復旧後はPVC UID、stream長、status件数、baseline eventが不変で、新規eventはattempt 1でdelivered、queue/pendingと30秒failure increaseは0、alertはinactive、専用clusterと一時resourceは0件となった。確認可能なdata loss、partial enqueue、false successは0件だった。
