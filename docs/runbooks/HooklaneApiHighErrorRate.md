# HooklaneApiHighErrorRate Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`API受付可用性`](../SLO.md#api受付可用性)
- Dashboard: `Hooklane SLI and Operations` の `API error rate`、`API acceptance success rate`、`Available API replicas`

## 影響

有効なevent requestが5xxとなり、API受付可用性の設計targetとerror budgetへ影響する。Redisへ永続化できていないrequestへ202を返してはならない。

## 確認するdashboard / metric

`hooklane_http_requests_total{service="api",route="/v1/events",status_class="5xx"}`、`hooklane_enqueue_total`、`hooklane_redis_operation_failures_total`、`hooklane_service_ready{service="api"}`を確認する。

## 最初の切り分け

API replicaがReadyか、5xxが全replicaか一部か、Redis failureが同時に増えているかを分ける。不正requestの4xxはこのalert原因に含めない。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployments,pods
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get endpointslices -l kubernetes.io/service-name=hooklane-api
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-api --tail=100
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get events --sort-by=.lastTimestamp
```

JSON logの`request_id`、`event_id`、`reason_code`だけで相関し、payloadやheader値を収集しない。

## 直近変更確認

`git log --oneline -20`とHelm release revisionを確認し、API、Redis、ConfigMap、probe変更を特定する。

## 暫定対応

直近の既知良好commitへ戻す判断を人間へescalateする。RedisがReadyでない場合はAPIを無理にready扱いせず、原因を修復する。dataを消す操作やforce rollbackは行わない。

## 復旧確認

API 2 replicaがReadyとなり、有効eventが202、最終状態が`delivered`、5xx rateがalert window外へ戻り、alertがinactiveになることを確認する。

```bash
make observability-smoke
```

## escalation条件

5xxが15分継続する、全API replicaがUnavailable、Redis data lossの疑い、復旧に破壊的操作またはsecret閲覧が必要な場合は人間へescalateする。

## 恒久対策候補

reason code別のerror budget分析、API dependency timeoutの調整、capacity test、再発したfailure modeのcontract test追加を検討する。

## known limitations

local kindは単一nodeで、外部load balancerや本番trafficを再現しない。短時間の5xx解消を30日SLO達成と解釈しない。
