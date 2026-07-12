# Blameless postmortem: worker stop recovery

- Incident record: [`Worker stop incident drill`](worker-stop.md)
- Alerts: `HooklaneQueueBacklogGrowing`、`HooklaneOldestEventTooOld`
- Runbook: [`HooklaneQueueBacklogGrowing`](../runbooks/HooklaneQueueBacklogGrowing.md)
- SLI/SLO: [`配送適時性`](../SLO.md#配送適時性)、[`queue backlogとoldest event age`](../SLO.md#queue-backlogとoldest-event-age)
- Dashboard: [`Hooklane SLI and Operations`](../../charts/hooklane/files/grafana/dashboards/hooklane-overview.json) の `Worker in-flight`、`Pending message count`、`Queue depth`、`Oldest queued event age`
- Alert rules: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)

## Summary

local kind drillで、downstream side effectの完了後かつRedis ack前にworkerを停止した。messageはconsumer groupのpendingとして保持され、replacement workerが同じevent IDをclaimしてattempt 2で配送を完了した。at-least-once契約どおりdownstreamは同じevent IDを2回観測したが、確認可能なaccepted event lossは0件だった。

## Impact

対象eventはworker不在中に`delivering`のまま滞留し、`hooklane_pending_messages`、`hooklane_queue_depth`、`hooklane_oldest_queued_event_age_seconds`が増加した。配送適時性は一時的に低下し、downstreamにはduplicate side effectの可能性が生じた。API受付、Redis上のevent/status、他の正常eventは失われなかった。

## Detection

`hooklane_worker_in_flight`が1の処理中にworkerが消失し、pending、queue depth、oldest ageが増加した。`HooklaneQueueBacklogGrowing`と`HooklaneOldestEventTooOld`がpendingとなり、dashboard、structured logのevent ID/attempt、Runbookからfailure windowを切り分けられた。

## Timeline

- T+00s: baseline eventがattempt 1でdeliveredとなり、通常状態を確認した。
- T+01s: drill eventを受付し、worker attempt 1とsink receiptを同じevent IDで確認した。
- T+02s: Redis ack前にworkerを停止し、対象Podだけを削除した。
- T+05s: status attempt 1、consumer pending 1、queue depth 1以上を確認した。
- T+30s以降: backlogとoldest-event alertがpendingとなった。
- 復旧開始: sinkを`accept`へ戻し、旧delay Podの終了後にreplacement workerを起動した。
- 復旧完了: replacementがattempt 2でclaimしてdeliveredとし、pending、queue、oldest age、in-flightが0、alertがinactiveへ戻った。

## Root cause

Hooklaneはdownstream responseを受けてからRedis messageをackするat-least-once方式である。downstream side effectとRedis ackは単一transactionではないため、その間にworker processが失われるとmessageはpendingに残り、回収時に同じevent IDを再配送する。このdrillはその設計上のfailure windowを意図的に再現した。

## Contributing factors

- local構成はworker 1 replicaであり、停止中に新規処理を引き継ぐworkerが存在しなかった。
- downstream side effectとqueue ackを跨ぐ分散transactionを使用していない。
- duplicate抑止はHooklane内部の配送回数ではなく、downstreamがevent IDをdeduplication keyとして扱う契約に依存する。
- local drill用の短いpending idle thresholdとalert thresholdを使用した。

## What went well

- Redis stream、status、pendingがworker消失後も保持され、event lossを防いだ。
- event IDとattemptを持つstructured logで停止前後を相関できた。
- queue、pending、oldest age、in-flight metricと2 alertが想定したfailure modeを示した。
- Runbookの手順どおりworkerを復旧し、自動claimと最終正常化を確認できた。

## What went poorly

- delivery side effectとackの間にはduplicateを完全には防げないwindowがある。
- 単一workerのため停止中は配送progressが止まり、backlogが増加した。
- local dashboard/alertは検知を確認できるが、外部通知やon-call応答時間を検証しない。

## Recovery

failure injection用sinkを`accept`へ戻し、旧sink Podの完全終了を確認してからworkerを1 replicaへ復旧した。replacement workerはidle thresholdを超えたpending messageを同じevent IDでclaimし、attempt 2で配送してackした。新規eventのattempt 1 delivery、全workload Ready、queue/pending/oldest/in-flight 0、alert inactiveを確認後、専用clusterを削除した。

## Corrective actions

event ID deduplication contract、pending recovery integration test、worker stop drill、queue/oldest alert、Runbook相互linkを継続的なquality gateとして維持する。今後の候補は複数worker時のclaim競合test、termination graceのcapacity test、downstream idempotency適合確認、長時間backlogのburn-rate設計である。

## Action categories and status

| Category | Action | Status |
|---|---|---|
| Guardrail | event IDをdownstream deduplication keyとして固定する | 完了 |
| Test | side effect後・ack前停止とpending claimを`make incident-worker-stop`で再現する | 完了 |
| Detection | queue、pending、oldest age、in-flightと対応alertを相互参照する | 完了 |
| Documentation | incident、Runbook、SLO、dashboard、postmortemをlinkする | 完了 |
| Resilience | 複数workerと長時間backlogの追加検証を設計する | 未着手 |

## Lessons learned

at-least-once recoveryの成功判定には最終`delivered`だけでなく、停止前後のevent ID、attempt、stream、pending、sink receiptを同時に照合する必要がある。duplicateは直ちにdata lossやfailureを意味せず、event IDによるdeduplication contractと対で評価する。復旧操作中もworkerを停止したままsinkの正常化を先に完了させることで、追加retryを避けられる。

## Limitations

単一node kind、単一Redis、単一worker、project内mock sinkの短時間drillである。複数worker競合、node/PV loss、network partition、外部downstreamの実dedup実装、本番alert通知、長期SLO、production capacityは未確認であり、このreceiptは本番incident実績ではない。
