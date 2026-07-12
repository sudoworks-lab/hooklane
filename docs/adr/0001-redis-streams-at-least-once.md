# ADR 0001: Redis Streamsとat-least-once delivery

## 状態

承認済み

## 背景

HooklaneはHTTP受付をdownstream配送から分離し、worker停止やretryable downstream failureの間もaccepted eventをqueueへ保持する必要がある。Docker Composeとsingle-node kindで再現でき、queue、status、retry、pending recoveryを一つのdependencyで検証できる構成が必要だった。

Delivery side effectとqueue acknowledgementを異なるsystemへ完全にatomic commitする仕組みはない。Workerがside effect後かつack前に停止するfailure windowを明示的に扱う必要がある。

## 決定

Redis Streams consumer groupをqueueに採用し、event status、retry schedule、dead-letter、idempotency mappingもRedisへ保持する。APIはeventと初期statusを原子的にenqueueできた場合だけ`202 Accepted`を返す。

Delivery guaranteeはat-least-onceとする。Workerは成功後にackし、未ack messageはpendingに残す。Replacement workerはidle threshold後にpending messageをclaimし、同じevent IDで再配送する。

Downstreamはevent IDをdeduplication keyとして扱う。APIの`Idempotency-Key` contractは受付重複を抑止するが、downstream side effectのduplicateを防ぐcontractとは分離する。

## 検討した代替案

- In-process queue: process restartでaccepted eventを失い、worker分離とpending recoveryを検証できないため採用しない。
- Immediate synchronous delivery: downstream latencyとfailureをAPI acceptanceへ結合し、retryとbacklogを分離できないため採用しない。
- Exactly-once claim: downstreamを含むdistributed transactionなしには保証できず、false guaranteeになるため採用しない。
- Kafkaなど別broker: ローカル構成のdependencyと運用複雑性が増え、必要contractに対して過剰なため採用しない。

## 結果

- Accepted eventはAPI processやworker processの停止だけでは失われず、pending recoveryを実装できる。
- Side effect後・ack前のduplicate deliveryは許容され、downstream event-ID deduplicationが必須になる。
- Retry、dead-letter、status、queue metricsをRedis stateと整合させられる。
- Single Redisがavailabilityとdurabilityのsingle point of failureになる。Replication、automatic failover、backupは別途必要である。
- PayloadとstatusにはTTLがなく、本番導入前にretention policyが必要である。

実装と実測は[Architecture](../ARCHITECTURE.md)、[worker-stop incident](../incidents/worker-stop.md)、[postmortem](../incidents/postmortem-worker-stop.md)を参照する。
