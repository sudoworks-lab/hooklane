# HooklaneRedisOperationFailures Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`API受付可用性`](../SLO.md#api受付可用性)
- Dashboard: `Hooklane SLI and Operations` の `Redis error`、`API acceptance success rate`、`Queue depth`
- Incident drill: [`Redis outage`](../incidents/redis-outage.md)

## 影響

API enqueue、status read、worker queue操作のいずれかが失敗し、受付不可、配送停止、状態参照不可となる可能性がある。

## 確認するdashboard / metric

`hooklane_redis_operation_failures_total`を`service`と`operation`別に確認し、API 5xx、worker readiness、queue signalと比較する。

## 最初の切り分け

Redis PodがReadyか、restartやPVC eventがあるか、APIだけかworkerも失敗しているかを分ける。Redis URLやcredential値は表示しない。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get statefulset/hooklane-redis pod/hooklane-redis-0 pvc
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane exec statefulset/hooklane-redis -- redis-cli ping
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs statefulset/hooklane-redis --tail=100
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get events --sort-by=.lastTimestamp
```

application logは分類済み`reason_code=redis_error`とoperationだけを使用する。

## 直近変更確認

`git log --oneline -20`でRedis image、resource、AOF/PVC、queue Lua、probe変更を確認する。

## 暫定対応

一時的resource pressureを解消し、StatefulSetが既存PVCでReadyへ戻ることを待つ。PVC削除、Redis key削除、database flush、password閲覧は行わない。

## 復旧確認

`redis-cli ping`、Redis readiness、API 202、worker delivery、queue depth 0を確認し、local 30秒window後にalertがinactiveになることを確認する。

```bash
make observability-smoke
```

## escalation条件

PVC mount failure、AOF corruption、data lossの疑い、15分超の停止、破壊的repairまたはsecret閲覧が必要な場合は人間へescalateする。

## 恒久対策候補

capacity planning、backup/restore設計、Redis HA、operation別SLO、fault injection環境の分離を検討する。これらは現行P0のNon-goalを超えるため別判断とする。

## known limitations

local kindは単一Redisと単一PVCで、replication、failover、node lossを扱わない。短時間復旧を本番可用性実績と解釈しない。
