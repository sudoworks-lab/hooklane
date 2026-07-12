# ADR 0003: ローカルkind、Helm、Prometheusの検証境界

## 状態

承認済み

## 背景

Hooklaneのchart、rolling update、observability、incident responseを、cloud accountやexternal registryなしで再現する必要がある。Application metricsをscrapeし、dashboardとalert ruleをGit管理する一方、Prometheus Operator導入によるCRDとcontroller dependencyは避けたい。

Local verificationはproduction availabilityや30日SLO実績を主張しない境界を明確にする必要がある。

## 決定

Project専用single-node kind clusterとHelm chartをruntime検証境界にする。Application imageはlocal buildしてkindへloadし、registryへpushしない。Observabilityはvaluesで有効化し、pinned PrometheusとGrafana imageを同じchartでdeployする。

Prometheus OperatorとServiceMonitorは採用しない。Prometheusの通常scrape configがannotated application Podをnamespace内でdiscoverする。必要なKubernetes API accessはshort-lived projected ServiceAccount tokenとnamespace read-only Roleへ限定する。

Dashboard JSON、datasource、alert rule、SLI PromQLはrepositoryで管理し、local smokeでtarget、metric変化、query、provisioning、alert recoveryを検証する。

## 検討した代替案

- Prometheus Operator: ServiceMonitorとlifecycle管理は得られるが、CRD、controller、追加image、upgrade boundaryがlocal scopeに対して大きいため採用しない。
- External managed monitoring: cloud credential、外部公開、課金、remote stateが必要になり、self-contained verificationを失うため採用しない。
- Docker Composeだけの検証: Kubernetes probe、PDB、RBAC、rolling strategy、rollbackを検証できないため採用しない。
- Multi-node production-like cluster: local resource costと複雑性が増え、今回のacceptance boundaryを越えるため採用しない。

## 結果

- Cloud accountなしでchart、dashboard、alert、rollout、incident drillを再現できる。
- Prometheus discovery RBACとscrape configurationをchart自身で保守する必要がある。
- Single-node kind、short retention、ephemeral Grafanaはproduction topologyやlong-term SLO measurementを表さない。
- Operator固有CRDを必要とせず、valuesでobservabilityを無効化できる。
- GitHub hosted Actionsは実行済み。external registry pathは対象外である。

運用手順は[Operations](../OPERATIONS.md#observability-flow)、SLO解釈は[SLO](../SLO.md)、制約は[Limitations](../LIMITATIONS.md)を参照する。
