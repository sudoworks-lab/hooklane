# ADR 0002: Liveness、readiness、metricsの責務分離

## 状態

承認済み

## 背景

Redis outage時にAPIがeventを永続化できない状態を`202 Accepted`として扱ってはならない。一方、dependency outageだけでhealthyなprocessをliveness failureとして再起動し続けると、原因を解消せず診断性を下げる。Rolling updateではshutdownを開始したPodを新規trafficから外し、in-flight処理をbounded drainする必要がある。

Health responseへmetricsやinternal exceptionを混在させない境界も必要だった。

## 決定

Endpointの意味を次のように分離する。

- Livenessはprocessがrequestへ応答できることだけを示す。Redis outageだけではfalseにしない。
- Readinessは新規workを安全に受けられることを示す。Redis接続不能またはshutdown開始時にfalseにする。
- Metricsは独立した`/metrics` endpointで公開し、health bodyへ含めない。

APIはreadinessを満たさずeventを永続化できない場合、`202 Accepted`を返さない。Kubernetes Serviceはreadinessに成功したAPI Podだけをendpointへ加える。Shutdown時はAPIが新規enqueueを停止し、workerはin-flight deliveryをtermination grace内でdrainする。

## 検討した代替案

- Redis outageでlivenessもfalse: restart loopがdependency failureを解消せず、診断と復旧を不安定にするため採用しない。
- 単一health endpoint: process health、traffic eligibility、metricsの責務が曖昧になり、false successを検出しにくいため採用しない。
- Shutdown中もreadyのまま: terminating Podへ新規requestが入り、rolling update時のfailure windowが広がるため採用しない。

## 結果

- Redis outage時はPodを再起動させずにService endpointから外し、API false successを防げる。
- Operatorはliveness、readiness、`hooklane_service_ready`、Redis failure metricを別々に解釈する必要がある。
- Rolling updateはReady Podを維持しながらtrafficを切り替えられる。
- Dependency-specific readinessはexternal availabilityの完全な保証ではなく、local contractの範囲に限られる。

実装境界は[Architecture](../ARCHITECTURE.md#health-semantics-and-graceful-shutdown)、検証receiptは[Redis outage](../incidents/redis-outage.md)と[Operations](../OPERATIONS.md#rolling-update-and-rollback)を参照する。
