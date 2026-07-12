# HooklaneQueueBacklogGrowing Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`queue backlogとoldest event age`](../SLO.md#queue-backlogとoldest-event-age)
- Dashboard: `Hooklane SLI and Operations` の `Queue depth`、`Pending message count`、`Worker in-flight`
- Incident drill: [`Worker stop`](../incidents/worker-stop.md)
- Blameless postmortem: [`Worker stop recovery`](../incidents/postmortem-worker-stop.md)

## 影響

accepted eventがunreadまたはpendingに残り、配送適時性が悪化する。短い処理中のdepth 1ではなく、30秒継続するbacklogを対象とする。

## 確認するdashboard / metric

`hooklane_queue_depth`、`hooklane_pending_messages`、`hooklane_worker_in_flight`、`hooklane_retry_scheduled_total`、`hooklane_oldest_queued_event_age_seconds`を確認する。

## 最初の切り分け

workerがReadyか、in-flightで進行中か、retry_scheduledか、mock sink failureか、Redis failureかを分ける。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployment/hooklane-worker pods
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployment/hooklane-mock-sink
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-worker --tail=100
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get events --sort-by=.lastTimestamp
```

`event_id`、`attempt`、`status`、`reason_code`で追跡し、payloadを出力しない。

## 直近変更確認

`git log --oneline -20`でworker、retry policy、queue Lua、mock sink、resource limitの変更を確認する。

## 暫定対応

failure injectionが有効なら、検証所有者が`mockSink.failureMode=accept`へ戻す。worker Pod削除はpending recovery contractを理解したうえで、今回のproject cluster内だけで行う。Redis key削除は行わない。

## 復旧確認

queue depthとpendingが0、oldest ageが0へ戻り、新規eventがattempt 1でdeliveredとなり、alertがinactiveになることを確認する。

```bash
make observability-smoke
```

## escalation条件

backlogが15分増加する、workerが再起動を繰り返す、pending回収で進展しない、data lossの疑い、破壊的操作が必要な場合は人間へescalateする。

## 恒久対策候補

capacity test、worker concurrency設計、retry storm抑制、queue retention、backlog burn-rate alertを検討する。F018ではHPAを追加しない。

## known limitations

単一workerと単一Redisを前提とし、multi-consumer容量やRedis Clusterを測定しない。alert名のGrowingはlocalでは持続backlogを近似する。
