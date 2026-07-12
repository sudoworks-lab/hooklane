# HooklaneRetryRateHigh Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`配送適時性`](../SLO.md#配送適時性)
- Dashboard: `Hooklane SLI and Operations` の `Retry count`、`Delivery success / failure`、`Oldest queued event age`

## 影響

retryable failureにより配送が遅れ、queueとoldest ageが増える可能性がある。retryはdata loss防止動作だが、継続増加は正常ではない。

## 確認するdashboard / metric

`hooklane_retry_scheduled_total`を`reason_code`別に確認し、`hooklane_delivery_outcomes_total`、queue depth、oldest ageと比較する。

## 最初の切り分け

HTTP 5xx/429、timeout、connection errorのどれか、単一eventか全eventか、mock sink failure injection中かを確認する。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get pods
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployment/hooklane-mock-sink -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="HOOKLANE_MOCK_SINK_MODE")].value}'
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-worker --tail=100
```

`event_id`、`attempt`、`reason_code`、`duration_ms`で確認する。

## 直近変更確認

`git log --oneline -20`でretry policy、maximum attempts、clock/jitter、sink behaviorの変更を確認する。

## 暫定対応

failure injectionを解除し、downstreamが回復するまで新しい実験投入を止める。retry scheduleやpending messageを手動削除しない。

## 復旧確認

retry counterの30秒increaseが0、新規eventがdelivered、queue depth/pendingが0、alertがinactiveになることを確認する。

```bash
make observability-smoke
```

## escalation条件

retryが15分継続する、dead-letterが増える、同じeventのattemptが上限へ近づく、downstream仕様判断が必要な場合は人間へescalateする。

## 恒久対策候補

backoff上限、jitter、429 Retry-After対応、retry budget、downstream capacity contractを検討する。

## known limitations

local alert windowは30秒で、production tuning値ではない。単一workerのretry挙動だけを測定する。
