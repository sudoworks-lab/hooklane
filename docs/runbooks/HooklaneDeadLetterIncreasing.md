# HooklaneDeadLetterIncreasing Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`配送成功率`](../SLO.md#配送成功率)
- Dashboard: `Hooklane SLI and Operations` の `Dead-letter count`、`Delivery success / failure`、`Retry count`

## 影響

accepted eventがpolicy上限または非retry failureで自動配送対象外となり、operator reviewが必要である。配送成功率のfailureとして扱う。

## 確認するdashboard / metric

`hooklane_dead_letter_total`、`hooklane_delivery_outcomes_total{outcome="dead_lettered"}`、retry count、reason codeを確認する。

## 最初の切り分け

非retry HTTP 4xxかattempt上限かを分け、同一reason codeの集中、mock sink mode、直前のretry増加を確認する。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get pods
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployment/hooklane-mock-sink -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="HOOKLANE_MOCK_SINK_MODE")].value}'
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-worker --tail=100
```

dead-letter eventはevent ID、attempt、reason codeだけで確認し、payloadやRedis内部keyを出力しない。

## 直近変更確認

`git log --oneline -20`でdownstream contract、retryability、maximum attempts、dead-letter Luaの変更を確認する。

## 暫定対応

新規failure injectionを停止し、原因を修復する。dead-letter streamの削除、元streamへの手動再投入、自動bulk replayは人間判断なしに行わない。

## 復旧確認

新規dead-letter increaseが止まり、新規正常eventがdelivered、関連failure alertがinactiveになることを確認する。

```bash
make observability-smoke
```

## escalation条件

複数eventがdead-letter、payload確認が必要、replay判断が必要、data corruptionまたはdownstream contract changeが疑われる場合は人間へescalateする。

## 恒久対策候補

安全なreview/replay interface、reason code別集計、downstream schema contract、operator承認付きrecovery手順を検討する。

## known limitations

F018は自動replayを実装しない。local Redisは単一instanceで、長期dead-letter retentionを検証しない。
