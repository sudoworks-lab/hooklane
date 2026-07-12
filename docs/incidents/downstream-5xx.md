# Downstream 5xx incident drill

- Command: `make incident-downstream-5xx`
- Alert: [`HooklaneDeliveryFailureRateHigh`](../runbooks/HooklaneDeliveryFailureRateHigh.md)、[`HooklaneRetryRateHigh`](../runbooks/HooklaneRetryRateHigh.md)、[`HooklaneQueueBacklogGrowing`](../runbooks/HooklaneQueueBacklogGrowing.md)
- SLI/SLO: [`配送成功率`](../SLO.md#配送成功率)、[`配送適時性`](../SLO.md#配送適時性)
- Dashboard: `Hooklane SLI and Operations` の `Delivery success / failure`、`Retry count`、`Queue depth`、`Oldest queued event age`

## 再現手順

専用kind clusterへobservability有効のHooklaneをdeployし、正常配送を確認する。Helmの`mockSink.failureMode`だけを一時的に`server_error`へ変更して3 eventを投入する。検証後は`accept`へ戻し、通常のretry policyに配送を再開させる。

## 期待する影響

APIの安全な202受付は継続するが、workerの配送attemptはHTTP 503で失敗し、eventは`retry_scheduled`となる。配送成功率と適時性が低下し、queue backlogが一時的に残る。

## 期待するmetrics

- `hooklane_delivery_outcomes_total{outcome="retry_scheduled",reason_code="http_5xx"}`が増加する。
- `hooklane_retry_scheduled_total{reason_code="http_5xx"}`が増加する。
- `hooklane_queue_depth`と`hooklane_oldest_queued_event_age_seconds`が0より大きくなる。
- 復旧後に`hooklane_queue_depth`、`hooklane_pending_messages`、oldest ageが0へ戻る。

## 期待するalert

`HooklaneDeliveryFailureRateHigh`と`HooklaneRetryRateHigh`がpendingまたはfiringになる。backlogが30秒継続する場合は`HooklaneQueueBacklogGrowing`も対象となる。alert annotationから対応Runbook、dashboard、SLOへ辿る。

## Structured log

workerの`delivery_started`と`retry_scheduled`、mock sinkの`delivery_received`を同じ`event_id`で照合する。`attempt`は1以上、`reason_code`は`http_5xx`である。payload、Idempotency-Key、Redis URL、credential、secret-like valueは収集しない。

## 初動切り分け

[`HooklaneDeliveryFailureRateHigh` Runbook](../runbooks/HooklaneDeliveryFailureRateHigh.md)に従い、workerとmock sinkのReady状態、sink mode、reason code、retry、queue depth、oldest ageを確認する。Redis failureやHTTP 4xxとは分類を分ける。

## 暫定対応

検証用の`server_error`が残っていれば、所有するHelm releaseだけを`mockSink.failureMode=accept`へ戻す。retry schedule、pending message、dead-letter streamを手動削除しない。

## 復旧手順

mock sinkを`accept`へ戻し、Deployment rolloutを待つ。retry対象eventが同じevent IDで再配送されるまで待ち、新規recovery eventを投入する。

## 復旧確認

注入した3 eventが`delivered`またはpolicyどおりterminal stateへ進み、新規eventがattempt 1で`delivered`となることを確認する。queue depth、pending、oldest ageが0、failure/retry alertがinactive、sink modeが`accept`であることを確認する。

## データ消失の有無

各accepted eventについてRedis stream在籍、status APIの同一event IDとattempt count、mock sinkのevent-ID-only receiptを照合する。3境界すべてが一致した場合だけdata lossなしと判定する。

## 再発防止候補

downstream contract test、timeout budget、reason-code別alert、retry storm抑制、circuit breaker設計、capacity testを候補とする。P0 drillでは自動的なretry policy変更を行わない。

## 制約と未確認事項

単一node kind、単一worker、project内mock sink、短いalert windowだけを検証する。外部downstream、本番traffic、30日SLO、通知経路、長期backlog、operatorによるdead-letter replayは未確認である。

## 検証receipt

2026-07-12T09:20:40+09:00に`make incident-downstream-5xx`を実行した。3 eventすべてでdelivery failureとretryが増え、DeliveryFailure/Retry alertはpending、queue depthとoldest ageは増加した。worker/sink structured log、Redis stream、status、復旧後receiptが同じevent IDで一致し、3 eventはattempt 2以上でdeliveredとなった。最終的にsinkは`accept`、queue depth/pending/oldest ageは0、両alertはinactive、専用clusterと一時resourceは0件となり、確認可能なdata lossは0件だった。
