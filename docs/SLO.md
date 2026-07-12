# Hooklane SLI / SLO設計目標

## この文書の位置づけ

ここに記載する数値は、Hooklaneの設計とalertを整合させるためのSLO設計目標。本番SLOの達成実績ではなく、単一nodeのローカルkind環境で行う短時間検証を30日間のSLO実績として扱わない。外部公開、本番traffic、複数zone、Redis HAを測定していない。

- 測定windowの設計値はrolling 30日とする。
- ローカルkindのPrometheus retentionは2時間であり、30日queryの実績値を生成しない。
- F018のfailure injectionはalert pathの検証であり、error budget消費実績ではない。
- dashboard正本は[`Hooklane SLI and Operations`](../charts/hooklane/files/grafana/dashboards/hooklane-overview.json)、PromQL対応表は[`observability/sli-promql.json`](../observability/sli-promql.json)とする。

設計boundaryと運用方法は[アーキテクチャ](ARCHITECTURE.md)、[運用](OPERATIONS.md)、[security](SECURITY.md)、[制約](LIMITATIONS.md)を参照する。

## API受付可用性

設計targetはrolling 30日で99.9%以上とする。有効なevent requestをRedis Streamsへ永続化し、`202 Accepted`を返したものを成功と数える。Redis operation failureまたは内部failureにより有効requestを受け付けられなかった場合は失敗と数える。

```promql
sum(rate(hooklane_enqueue_total{service="api",outcome="success"}[30d]))
/
clamp_min(
  sum(rate(hooklane_enqueue_total{service="api",reason_code!="idempotency_conflict"}[30d])),
  0.000000001
)
```

不正JSON、schema validation failure、必須field不足、未知event IDの参照は利用者入力のinvalid requestであり、API受付可用性の分母へ含めない。同一`Idempotency-Key`を異なる内容へ再利用した409もvalid acceptance attemptではないため除外する。API processの5xx rateは補助signalとして[`HooklaneApiHighErrorRate`](runbooks/HooklaneApiHighErrorRate.md)で監視する。

## API受付遅延

設計targetは、有効なeventを受け付けて202を返すrequestのp95がrolling 30日で250ms未満であることとする。local dashboardでは短いwindowで傾向だけを表示する。

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(hooklane_http_request_duration_seconds_bucket{
      service="api",
      route="/v1/events",
      method="POST",
      status_class="2xx"
    }[30d])
  )
)
```

このhistogramはHTTP boundaryから202 responseまでを測り、worker配送時間を含めない。Redis enqueue latencyの悪化はAPI latencyとAPI readiness、Redis failureを合わせて判断する。

## 配送成功率

設計targetはrolling 30日で99.0%以上とする。最終的に`delivered`となったeventを成功、policy上限または非retry failureで`dead_letter`となったeventを失敗とする。個々のretry attemptをevent単位の分母へ重複計上しない。

```promql
sum(rate(hooklane_delivery_completion_total{service="worker"}[30d]))
/
clamp_min(
  sum(rate(hooklane_delivery_completion_total{service="worker"}[30d]))
  + sum(rate(hooklane_dead_letter_total{service="worker"}[30d])),
  0.000000001
)
```

`hooklane_delivery_outcomes_total`はattempt単位の即時診断signalであり、SLOのevent単位分母には使わない。高いfailure rateは[`HooklaneDeliveryFailureRateHigh`](runbooks/HooklaneDeliveryFailureRateHigh.md)、dead-letter発生は[`HooklaneDeadLetterIncreasing`](runbooks/HooklaneDeadLetterIncreasing.md)で検知する。

## 配送適時性

設計targetは、受け付けたeventの99.0%以上がrolling 30日で60秒以内に`delivered`となることとする。accepted timestampからdelivery完了までを`hooklane_delivery_end_to_end_duration_seconds`と`hooklane_delivery_completion_total`で測る。

```promql
sum(rate(hooklane_delivery_completion_total{
  service="worker",
  outcome="within_60_seconds"
}[30d]))
/
clamp_min(sum(rate(hooklane_enqueue_total{service="api",outcome="success"}[30d])), 0.000000001)
```

retry中、pending中、dead-letterとなったeventは60秒以内完了の分子へ入らない。oldest ageは[`HooklaneOldestEventTooOld`](runbooks/HooklaneOldestEventTooOld.md)、retry増加は[`HooklaneRetryRateHigh`](runbooks/HooklaneRetryRateHigh.md)で検知する。

## queue backlogとoldest event age

queue signalはSLOそのものではなく、配送適時性を先行検知する運用SLIとする。

- `hooklane_queue_depth`: unreadとconsumer-group pendingの合計。通常のlocal正常配送後は0へ戻る。
- `hooklane_oldest_queued_event_age_seconds`: unreadまたはpendingの最古event age。20秒超をlocal alert thresholdとする。
- `hooklane_pending_messages`: consumer groupが取得済みで未ackのmessage数。

queue depthが30秒継続して0より大きい場合は[`HooklaneQueueBacklogGrowing`](runbooks/HooklaneQueueBacklogGrowing.md)を確認する。単一eventの短時間処理で一時的に1となること自体は障害と断定しない。

## error budget

rolling 30日の設計budgetは次のように解釈する。

- API受付可用性99.9%では、有効requestの0.1%までが失敗budgetである。
- 配送成功率99.0%では、最終結果eventの1.0%までがdead-letter budgetである。
- 配送適時性99.0%では、受け付けeventの1.0%までが60秒超過または未完了budgetである。

短いwindowのalertはerror budget burn-rateの完成版ではない。local検証ではsignal、rule load、pending/firing、復旧を確認するだけで、30日budget残量を報告しない。

## downstream障害期間の扱い

project内mock sinkの5xx、timeout、connection failure期間も、配送成功率と配送適時性の測定から除外しない。downstream障害はretryの理由であり、利用者から見た配送未完了であるためである。一方、APIがRedisへ安全にenqueueできている期間は、downstream 5xxだけを理由にAPI受付可用性の失敗へ数えない。

Redis停止またはRedis operation failureによりAPIが有効requestを永続化できない期間はAPI受付可用性へ影響する。対応手順は[`HooklaneRedisOperationFailures`](runbooks/HooklaneRedisOperationFailures.md)を参照する。

## ローカルkind測定と本番実績の違い

ローカルkindは単一node、単一worker、単一Redis、project内mock sink、短いretentionを使う。次を検証していない。

- 複数nodeまたは複数zoneの可用性
- Redis replication、failover、managed service
- 本番traffic pattern、長期容量、30日retention
- 外部downstreamの実network特性
- alert通知先、on-call、escalationの実運用時間

したがってdashboard screenshotや短時間queryを本番SLO達成の証拠として使用しない。target変更はmetric contract、alert threshold、dashboard、Runbookを同じchangeで見直す。

## alertとRunbookの対応

| Signal | Alert | Runbook |
|---|---|---|
| API 5xx | `HooklaneApiHighErrorRate` | [`HooklaneApiHighErrorRate.md`](runbooks/HooklaneApiHighErrorRate.md) |
| queue depth | `HooklaneQueueBacklogGrowing` | [`HooklaneQueueBacklogGrowing.md`](runbooks/HooklaneQueueBacklogGrowing.md) |
| oldest age | `HooklaneOldestEventTooOld` | [`HooklaneOldestEventTooOld.md`](runbooks/HooklaneOldestEventTooOld.md) |
| delivery failure | `HooklaneDeliveryFailureRateHigh` | [`HooklaneDeliveryFailureRateHigh.md`](runbooks/HooklaneDeliveryFailureRateHigh.md) |
| retry | `HooklaneRetryRateHigh` | [`HooklaneRetryRateHigh.md`](runbooks/HooklaneRetryRateHigh.md) |
| dead-letter | `HooklaneDeadLetterIncreasing` | [`HooklaneDeadLetterIncreasing.md`](runbooks/HooklaneDeadLetterIncreasing.md) |
| Redis failure | `HooklaneRedisOperationFailures` | [`HooklaneRedisOperationFailures.md`](runbooks/HooklaneRedisOperationFailures.md) |

## incident drillとの対応

設計目標とSLIのfailure modeは、ローカルkind上の[`Downstream 5xx`](incidents/downstream-5xx.md)、[`Redis outage`](incidents/redis-outage.md)、[`Worker stop`](incidents/worker-stop.md)で再現する。3 drillは`make incident-smoke`で集約し、検知、Runbookによる切り分け、復旧、確認可能なaccepted event loss 0を検査する。worker停止時のat-least-once recoveryとduplicate可能性は[`blameless postmortem`](incidents/postmortem-worker-stop.md)へ記録する。これらは短時間のローカル検証receiptであり、本番SLO達成実績ではない。
