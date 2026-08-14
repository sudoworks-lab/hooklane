# Worker stop incident drill

- Command: `make incident-worker-stop`
- Alert: [`HooklaneWorkerUnavailable`](../runbooks/HooklaneWorkerUnavailable.md)、[`HooklaneQueueBacklogGrowing`](../runbooks/HooklaneQueueBacklogGrowing.md)、[`HooklaneOldestEventTooOld`](../runbooks/HooklaneOldestEventTooOld.md)
- SLI/SLO: [`配送適時性`](../SLO.md#配送適時性)、[`queue backlogとoldest event age`](../SLO.md#queue-backlogとoldest-event-age)
- Dashboard: `Hooklane SLI and Operations` の `Worker in-flight`、`Pending message count`、`Queue depth`、`Oldest queued event age`
- Blameless postmortem: [`Worker stop recovery`](postmortem-worker-stop.md)

## 再現手順

mock sinkを`post_receipt_delay`へ変更し、event-ID-only receiptを記録してからHTTP responseを20秒遅らせる。workerがdelivery中、sink side effect後、Redis ack前の状態でworker Deploymentをreplica 0へscaleし、対象Podだけをgrace period 0で削除する。pendingを保持したままbacklog signalを確認し、replica 1へ戻す。

## 期待する影響

accepted eventはstatus `delivering`、attempt 1、Redis consumer group pending 1に残る。worker不在中はqueue depthとoldest ageが増え、配送適時性が低下する。side effect後・ack前停止のため、replacement workerによる同じevent IDの再配送が発生し得る。

## 期待するmetrics

- 停止前に`hooklane_worker_in_flight`が1、`hooklane_delivery_attempts_total`が増加する。
- 停止後に`absent(up{job="hooklane-applications",component="worker"})`が1となり、worker scrape targetのseries消失を示す。
- 停止後に`hooklane_pending_messages`と`hooklane_queue_depth`が1以上、`hooklane_oldest_queued_event_age_seconds`が20秒を超える。
- 復旧後にdelivery successが増え、worker in-flight、pending、queue depth、oldest ageが0へ戻る。

## 期待するalert

`HooklaneWorkerUnavailable`、`HooklaneQueueBacklogGrowing`、`HooklaneOldestEventTooOld`がpendingまたはfiringになる。service availability、queue backlog、oldest ageの責務を混ぜず、各alert annotationから対応Runbook、dashboard、SLOへ辿る。

## Structured log

停止前workerの`delivery_started` attempt 1、replacement workerの`delivery_started`と`delivery_completed` attempt 2、mock sinkの同一event IDに対する複数`delivery_received`を照合する。payload、credential、Redis URL、secret-like valueは収集しない。

## 初動切り分け

[`HooklaneWorkerUnavailable` Runbook](../runbooks/HooklaneWorkerUnavailable.md)と[`HooklaneQueueBacklogGrowing` Runbook](../runbooks/HooklaneQueueBacklogGrowing.md)に従い、worker Pod / scrape target / readiness、consumer pending、in-flight、queue depth、oldest age、sink mode、Redis Readyを確認する。downstream 5xx、Redis outage、正常graceful drainとは分類を分ける。

## 暫定対応

worker Deploymentをreplica 1へ戻し、pending idle threshold後の自動claimを待つ。pending messageを手動ack/deleteせず、event statusやattempt countを書き換えない。

## 復旧手順

replacement workerをReadyにし、同じevent IDがattempt 2以上で再配送されることを確認する。duplicate受信はdownstreamのevent IDをdeduplication keyとして一意なreceiptへまとめる。検証後はsinkを`accept`、worker replica 1、通常retry/pending設定へ戻す。

## 復旧確認

対象eventがdeliveredまたはpolicyどおりterminal state、pending/queue/oldest/in-flightが0、worker targetがUPかつReady、新規eventがattempt 1でdelivered、availability/backlog/oldest alertがinactiveであることを確認する。

## データ消失の有無

Redis stream、status API、attempt transition、停止前後worker log、mock sink receipt logを同じevent IDで照合する。accepted eventがstreamに残りattempt 2以上でterminal stateへ進めばdata lossなしと判定する。複数delivery logはat-least-onceの重複可能性であり、event ID一意receipt contractと区別する。

## 再発防止候補

termination graceのcapacity test、delivery lease、pending idle tuning、idempotent downstream contract、event-ID deduplication、worker replica/concurrency設計、backlog burn-rate alertを候補とする。

## 制約と未確認事項

単一worker、単一Redis、単一node kind、project内mock sinkだけを検証する。複数worker競合、network partition、node loss、本番downstreamのdedup実装、長時間backlogは未確認である。

## 検証receipt

2026-07-12T10:16:42+09:00に`make incident-worker-stop`を実行してpassした。baselineと復旧後の新規eventはattempt 1でdeliveredとなった。障害対象eventはsink side effect後・ack前にworkerを停止してstatus attempt 1、pending 1、queue depth 1以上、oldest age 20秒超を保持し、`HooklaneQueueBacklogGrowing`と`HooklaneOldestEventTooOld`はいずれもpendingになった。replacement workerは同じevent IDをattempt 2でclaimしてdeliveredとし、sinkは同じevent IDの配送を2回観測した一方、event-ID-only receipt contractは一意性を維持した。Redis stream、status、worker log、sink logを照合したaccepted event lossは0件で、最終的にpending、queue depth、oldest age、worker in-flightは0、両alertはinactive、sinkは`accept`、workerはReadyへ戻った。終了時に専用kind clusterを削除した。
