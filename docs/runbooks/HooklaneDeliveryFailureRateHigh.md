# HooklaneDeliveryFailureRateHigh Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`配送成功率`](../SLO.md#配送成功率)
- Dashboard: `Hooklane SLI and Operations` の `Delivery success / failure`、`Delivery p95 latency`、`Retry count`
- Incident drill: [`Downstream 5xx`](../incidents/downstream-5xx.md)

## 影響

workerの配送attemptがretry、pending、dead-letter、内部failureへ偏り、配送成功率と適時性を損なう。

## 確認するdashboard / metric

`hooklane_delivery_outcomes_total`、`hooklane_delivery_attempts_total`、`hooklane_delivery_duration_seconds`、`hooklane_retry_scheduled_total`をreason code別に確認する。

## 最初の切り分け

`http_5xx`、`http_429`、timeout、connection error、`http_4xx`、Redis errorのどれかを分類し、mock sink modeとworker readinessを確認する。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployment/hooklane-worker deployment/hooklane-mock-sink
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployment/hooklane-mock-sink -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="HOOKLANE_MOCK_SINK_MODE")].value}'
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-worker --tail=100
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-mock-sink --tail=100
```

arbitrary exception messageではなく`reason_code`を使い、payloadを収集しない。

## 直近変更確認

`git log --oneline -20`でsink mode、client timeout、retry分類、attempt上限、network関連変更を確認する。

## 暫定対応

検証用`server_error`が残っている場合は、所有者がHelm valuesを`accept`へ戻す。retryを無制限化せず、dead-letter dataを削除しない。

## 復旧確認

新規eventがdelivered、queue depth 0、failure rateが30秒window外へ戻り、alertがinactiveになることを確認する。

```bash
make observability-smoke
```

## escalation条件

failure rateが15分継続する、全eventが失敗する、downstream contract変更が疑われる、破壊的なqueue操作が必要な場合は人間へescalateする。

## 恒久対策候補

downstream contract test、timeout budget、circuit breaker設計、retry classification改善、reason code別alertを検討する。

## known limitations

alertはattempt単位の短時間比率で、event単位30日配送SLOとは異なる。local mock sink以外の外部downstreamを検証していない。
