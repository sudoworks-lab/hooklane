# HooklaneApiUnavailable Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`service availability`](../SLO.md#service-availability)
- Dashboard: `Hooklane SLI and Operations` の `Available API replicas`、`API acceptance success rate`

## 影響

現行local contractのAPI 2 replicaのうち、scrape可能かつapplication readinessがtrueのinstanceが2未満である。部分的なreplica lossもcriticalとして扱い、全target消失時はAPI受付が利用できない可能性がある。

## 確認するdashboard / metric

Prometheus target page、`up{job="hooklane-applications",component="api"}`、`hooklane_service_ready{service="api"}`、dashboardの`Available API replicas`を確認する。API 5xx、Redis failure、queue signalはそれぞれのalertで別に判断する。

## 最初の切り分け

API Podが2つ存在するか、各PodがRunning / Readyか、Prometheus targetが存在してUPか、`/health/ready`が成功するかを順に確認する。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployment/hooklane-api pods -l app.kubernetes.io/component=api
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get endpointslices -l kubernetes.io/service-name=hooklane-api
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-api --tail=100
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get events --sort-by=.lastTimestamp
```

payload、credential、header値を収集せず、Pod condition、probe failure、bounded reason codeを確認する。

## 直近変更確認

`git log --oneline -20`とHelm release revisionを確認し、API image、replica数、resource、probe、Prometheus annotation、Redis接続の変更を特定する。

## 暫定対応

失敗したrolloutまたは設定変更が明確な場合は、既知の正常revisionへ戻す判断を人間へescalateする。PodやRedis dataを強制削除せず、readinessを無効化して見かけ上Readyにしない。

## 復旧確認

API 2 replicaがRunning / Ready、両targetの`up`と`hooklane_service_ready`が1、有効eventが202後にdeliveredとなり、alertがinactiveへ戻ることを確認する。

```bash
make observability-smoke
```

## escalation条件

全API targetが消失する、15分以上2 replicaへ戻らない、Redis data lossが疑われる、復旧にsecret閲覧または破壊的操作が必要な場合は人間へescalateする。

## 恒久対策候補

再発したprobe / rollout failureのcontract test、resource capacity確認、local replica contractとalert thresholdの同時見直しを検討する。

## known limitations

threshold 2は現行chartのlocal `api.replicaCount`に対応する。single-node kindの短時間検知であり、multi-node / multi-zone HA、外部load balancer、本番traffic、30日SLO達成を保証しない。
