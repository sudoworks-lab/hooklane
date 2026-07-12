# HooklaneOldestEventTooOld Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`配送適時性`](../SLO.md#配送適時性)
- Dashboard: `Hooklane SLI and Operations` の `Oldest queued event age`、`Delivery within 60 seconds`、`Queue depth`

## 影響

最古eventが20秒を超え、60秒以内配送の設計targetが危険な状態である。retry、pending recovery、downstream遅延のいずれでも発生する。

## 確認するdashboard / metric

`hooklane_oldest_queued_event_age_seconds`、`hooklane_queue_depth`、`hooklane_pending_messages`、`hooklane_delivery_completion_total`を確認する。

## 最初の切り分け

oldest ageがworkerとAPIの両方で一致するか、pendingかunreadか、worker in-flightが停止しているか、retry countが増えているかを確認する。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get pods
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane rollout status deployment/hooklane-worker --timeout=60s
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-worker --tail=100
```

logのevent IDとattemptを使い、古いeventのpayloadを取得しない。

## 直近変更確認

`git log --oneline -20`でretry delay、pending idle threshold、worker resource、sink delayの変更を確認する。

## 暫定対応

downstream failure injectionを解除し、workerがReadyであることを確認する。無理なRedis message削除やstatus書換えは行わない。

## 復旧確認

oldest ageとqueue depthが0へ戻り、該当eventまたは新規eventがdeliveredとなり、alertがinactiveになることを確認する。

```bash
make observability-smoke
```

## escalation条件

oldest ageが60秒を超える、attemptが増えない、worker recoveryが失敗する、dead-letterへ大量移動する場合は人間へescalateする。

## 恒久対策候補

retry ceiling、downstream timeout、worker concurrency、age-based priority、SLO burn-rate alertを検討する。

## known limitations

accepted timestampはapplication clockを使う。local kindではclock skew、multi-node scheduling、長期backlogを検証していない。
