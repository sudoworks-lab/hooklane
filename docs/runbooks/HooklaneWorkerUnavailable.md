# HooklaneWorkerUnavailable Runbook

- Alert rule: [`hooklane-alerts.yml`](../../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)
- SLI/SLO: [`service availability`](../SLO.md#service-availability)
- Dashboard: `Hooklane SLI and Operations` の `Worker in-flight`、`Queue depth`、`Oldest queued event age`
- Incident drill: [`Worker stop`](../incidents/worker-stop.md)

## 影響

現行local contractのworker 1 replicaについて、scrape targetがdownまたは消失したか、application readinessがfalseである。accepted eventはRedisに保持され得るが、worker復旧まで配送は進行しない。

## 確認するdashboard / metric

Prometheus target page、`up{job="hooklane-applications",component="worker"}`、`hooklane_service_ready{service="worker"}`を主signalとして確認する。queue、pending、oldest ageは配送影響を示す別signalとして確認する。

## 最初の切り分け

worker Podが存在するか、Running / Readyか、Prometheus targetが存在してUPか、worker readiness commandが成功するかを順に確認する。downstream 5xx、Redis failure、queue backlogとは原因を断定せず分けて調べる。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get deployment/hooklane-worker pods -l app.kubernetes.io/component=worker
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane exec deployment/hooklane-worker -- python -m hooklane.worker.health ready
```

## logs / events / status確認

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-worker --tail=100
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get events --sort-by=.lastTimestamp
```

event statusは既知のevent IDで確認し、payload、credential、Redis raw dataを収集しない。

## 直近変更確認

`git log --oneline -20`とHelm release revisionを確認し、worker image、replica数、resource、probe、Prometheus annotation、Redis / downstream設定の変更を特定する。

## 暫定対応

failure injectionでreplica 0になっている場合は、そのdrillの所有者がreplica 1へ戻す。通常障害では既知の正常設定への復旧を人間と判断し、pending messageの手動ack / deleteやreadiness無効化は行わない。

## 復旧確認

worker 1 replicaがRunning / Ready、targetの`up`と`hooklane_service_ready`が1、alertがinactive、queue depth / pending / oldest ageが0、新規eventがattempt 1でdeliveredとなることを確認する。

```bash
make incident-worker-stop
```

## escalation条件

workerが15分以上復旧しない、再起動を繰り返す、pending claimが進まない、data lossが疑われる、secret閲覧または破壊的操作が必要な場合は人間へescalateする。

## 恒久対策候補

再発したstartup / readiness failureのcontract test、worker resource capacity、pending recovery tuning、複数worker構成の別設計を検討する。

## known limitations

threshold 1は現行chartのsingle-worker local構成に対応する。alertは原因を断定せず、auto remediation、notification destination、production HA、30日SLO達成を提供しない。
