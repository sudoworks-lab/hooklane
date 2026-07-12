# Hooklane operations

## Scope and prerequisites

この文書はlocal Docker Composeとproject専用kind clusterの再現手順である。本番運用手順ではない。必要toolと固定versionは[`toolchain.toml`](../toolchain.toml)を正本とし、Docker daemonを利用できることを前提にする。Commandはrepository rootから実行する。

初期化と環境診断は次の順で行う。

```bash
bash scripts/init.sh
make doctor
```

`scripts/init.sh`はproject-local virtual environmentとlock済みPython dependencyを準備する。`make doctor`は必要tool、Docker daemon、resourceを検査し、環境を変更しない。Failure時は表示された不足toolまたはresourceを解消してから先へ進む。System package managerやsudoを前提にしない。

## Make interface

| Target | Purpose | Prerequisite and cleanup |
|---|---|---|
| `make doctor` | local toolとresourceのpreflight | cleanup不要 |
| `make images-build` | API、worker、mock sink imageをlocal build | local imageを作成する |
| `make compose-up` | Compose serviceをbuildしてReadyまで待つ | 終了時`make compose-down` |
| `make smoke` | health、202受付、delivered statusを確認 | Compose起動済み |
| `make e2e-local` | idempotency、retry、dead-letter、pending recoveryを確認 | Compose起動済み、終了後`make compose-down` |
| `make lint` | Ruff | init済み |
| `make typecheck` | strict mypy | init済み |
| `make test` | unitと実Redis integration test | test用Redisはrunnerがcleanupする |
| `make verify` | current quality、security、chart、docs gate | image buildとscanner DB accessを含む |
| `make security` | Gitleaks、OSV-Scanner、Trivy filesystem/image scan | local scannerとDocker daemon |
| `make chart-validate` | Helm、values schema、Kubernetes policyを検証 | HelmとKubeconform |
| `make cluster-up` | project専用kind clusterを作成 | 終了時`make cluster-down` |
| `make deploy` | local imageをloadしHelm releaseをinstall/upgrade | cluster起動済み |
| `make chart-smoke` | Helm testとchart runtime contract | deploy済み |
| `make diagnostics` | sanitized workload、event、statusを確認 | cluster起動済み |
| `make observability-up` | clusterを必要なら作成し、Hooklane、Prometheus、Grafanaをdeploy | 終了時`make observability-down` |
| `make observability-smoke` | targets、metrics、dashboard、alerts、復旧を確認 | observability-up済み |
| `make e2e-kind` | 正常配送、idempotency、retry、pending recoveryを総合検証 | clusterがなければ作成し、所有したclusterだけcleanup |
| `make rollout-smoke` | rolling update、worker drain、bad release、rollbackを検証 | deploy済みcluster、終了時`make cluster-down` |
| `make incident-smoke` | downstream、Redis、workerの3 incident drillを実行 | clusterがなければ作成し、所有したclusterだけcleanup |

Targetの正本は[`Makefile`](../Makefile)である。Long-running targetは失敗を成功扱いせず、sanitized diagnosticsを残す場合がある。Failure後は内容を確認し、今回の検証で作られた`artifacts/kind-e2e-*`だけを除去してからrepository hygieneを確認する。

## Local Compose flow

最短のlocal flowは次の通りである。

```bash
make compose-up
make smoke
make e2e-local
make compose-down
```

Expected resultはAPI、worker、mock sink、Redisがhealthyになり、smoke eventが`202 Accepted`後に`delivered`となること、idempotencyとfailure recoveryのcontractがpassすることである。`make compose-down`はHooklane Compose projectのcontainer、network、volumeだけを削除する。途中失敗時も同targetでcleanupし、無関係なDocker resourceへ触れない。

Compose APIはloopbackの`127.0.0.1:18080`へ公開される。Health、status、metricsの責務は分離されている。

```bash
curl --fail http://127.0.0.1:18080/health/live
curl --fail http://127.0.0.1:18080/health/ready
curl --fail http://127.0.0.1:18080/metrics
```

Event requestは[`scripts/local_e2e.py`](../scripts/local_e2e.py)がpayloadをstdoutへ出さずに作成するため、manual exampleを複製するより`make smoke`を正本とする。

## Quality, security, and chart gates

変更前後のlocal gateは次を使う。

```bash
make lint
make typecheck
make test
make security
make chart-validate
make verify
```

`make verify`は個別gateを順に実行し、どれかがnon-zeroなら終了もnon-zeroになる。Security scanner unavailable、database取得失敗、timeout、parse failureはfinding 0と区別し、fail-closedとする。Scanner設定は[`security-policy.json`](../security-policy.json)、container例外は[`container-policy.json`](../container-policy.json)を参照する。

## Basic kind and Helm flow

ApplicationだけをdeployしてHelm testまで確認する。

```bash
make cluster-up
make deploy
make chart-smoke
make cluster-down
```

Expected resultはAPI 2 replica、worker、mock sink、RedisがReadyになり、Helm testが202受付とdeliveryを確認することである。`make deploy`はlocal image buildとkind loadを使い、external registryへpushしない。Failure時はcleanup前に`make diagnostics`を実行する。

Project識別子とdedicated kubeconfigは[`scripts/kind_runtime.py`](../scripts/kind_runtime.py)と[`deploy/kind/cluster.yaml`](../deploy/kind/cluster.yaml)を正本とする。`make cluster-down`はHooklane専用clusterとそのdedicated kubeconfigだけを削除する。

## Observability flow

Prometheus、Grafana、application target、dashboard、alert ruleをまとめて検証する。

```bash
make observability-up
make observability-smoke
make observability-down
```

Expected resultは全targetがUP、event deliveryでmetricsが増加、queue depthとpendingが0へ戻り、dashboardがprovisionされ、failure injection後のalertが回復することである。`make observability-down`はproject clusterを削除する。Dashboardは[Hooklane SLI and Operations](../charts/hooklane/files/grafana/dashboards/hooklane-overview.json)、alertは[rule file](../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)を正本とする。

Manual inspectionが必要な場合は、project kubeconfigを明示してcluster-local Serviceをloopbackへ一時forwardする。

```bash
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane port-forward service/hooklane-prometheus 19090:9090
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane port-forward service/hooklane-grafana 13000:3000
```

Prometheusは`http://127.0.0.1:19090/targets`と`/alerts`、Grafanaは`http://127.0.0.1:13000`で確認する。Port-forward processは確認後に停止する。Grafanaのanonymous Viewerはlocal cluster内だけの検証例外であり、外部へ公開しない。

## Logs, metrics, alerts, and event status

Application logはJSON 1行で、request IDまたはevent ID、attempt、outcome、reason codeを使って追跡する。Payloadやcredentialを含むRedis streamのraw dumpは診断手順にしない。

```bash
make diagnostics
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-api --tail=100
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane logs deployment/hooklane-worker --tail=100
kubectl --kubeconfig /tmp/hooklane-f014-kubeconfig --context kind-hooklane-f014 -n hooklane get events --sort-by=.lastTimestamp
```

Status APIは既知のevent IDについて`queued`、`delivering`、`retry_scheduled`、`delivered`、`dead_letter`とattempt countを返す。Composeでは次の形で確認する。

```bash
curl --fail http://127.0.0.1:18080/v1/events/<event-id>
```

Dead-letterは`hooklane_dead_letter_total`、`HooklaneDeadLetterIncreasing`、既知eventのstatusを突き合わせる。Recovery後は`hooklane_queue_depth`、`hooklane_pending_messages`、`hooklane_worker_in_flight`が0、該当alertがinactive、新規eventがdeliveredであることを確認する。Dead-letter replayの管理interfaceは実装していないため、raw queueを直接変更しない。

## Alert and Runbook index

| Alert | Primary signal | Runbook |
|---|---|---|
| `HooklaneApiHighErrorRate` | `hooklane_http_requests_total` | [API high error rate](runbooks/HooklaneApiHighErrorRate.md) |
| `HooklaneQueueBacklogGrowing` | `hooklane_queue_depth` | [Queue backlog](runbooks/HooklaneQueueBacklogGrowing.md) |
| `HooklaneOldestEventTooOld` | `hooklane_oldest_queued_event_age_seconds` | [Oldest event](runbooks/HooklaneOldestEventTooOld.md) |
| `HooklaneDeliveryFailureRateHigh` | `hooklane_delivery_outcomes_total` | [Delivery failure](runbooks/HooklaneDeliveryFailureRateHigh.md) |
| `HooklaneRetryRateHigh` | `hooklane_retry_scheduled_total` | [Retry rate](runbooks/HooklaneRetryRateHigh.md) |
| `HooklaneDeadLetterIncreasing` | `hooklane_dead_letter_total` | [Dead letter](runbooks/HooklaneDeadLetterIncreasing.md) |
| `HooklaneRedisOperationFailures` | `hooklane_redis_operation_failures_total` | [Redis operation failure](runbooks/HooklaneRedisOperationFailures.md) |

SLI、threshold、measurement windowは[SLO design target](SLO.md)を参照する。

## Incident and postmortem index

`make incident-smoke`は3 drillを順に実行し、正常復旧とcluster cleanupを確認する。

- [Downstream 5xx incident](incidents/downstream-5xx.md)
- [Redis outage incident](incidents/redis-outage.md)
- [Worker stop incident](incidents/worker-stop.md)
- [Worker stop blameless postmortem](incidents/postmortem-worker-stop.md)

個別drillは`make incident-downstream-5xx`、`make incident-redis-outage`、`make incident-worker-stop`で再現できる。既存cluster上で実行した場合はclusterを所有しないため自動削除しない。終了時にfailure injectionが通常設定へ戻り、queue、pending、in-flightが0、alertsがinactiveであることを確認してから`make cluster-down`を実行する。

## Rolling update and rollback

Rollout検証はdeploy済みclusterで行う。

```bash
make cluster-up
make deploy
make rollout-smoke
make cluster-down
```

`make rollout-smoke`はAPIのrolling strategy、継続request、worker graceful shutdown、intentional bad releaseのfailure検知、Helm rollback、復旧後deliveryを検査する。Bad releaseやfailure injectionはfinally pathでも正常revisionへ戻す。Failure時はsanitized diagnosticsを取得し、release history、Pod events、readinessを確認してからcleanupする。Irreversible database migrationはこの検証範囲に含めない。

## Common failures and first checks

| Symptom | First checks | Recovery condition |
|---|---|---|
| Docker command fails | `make doctor`、Docker daemon、disk/memory | doctorがpassする |
| Compose service not healthy | `docker compose ps`、service log、`make compose-down`後の再作成 | 全service healthy、smoke pass |
| API not ready | Redis Pod/connection、API log、`hooklane_service_ready` | readiness 200、新規delivery成功 |
| Queue grows | worker Ready、sink mode、pending/oldest metrics、Queue Runbook | queue/pending/oldestが0 |
| Delivery failures | sink status、reason code、retry/dead-letter metrics | sink accept、新規event delivered、alert inactive |
| Helm validation fails | rendered manifest、values schema、Kubeconform output | `make chart-validate` pass |
| Pod rollout stalls | `make diagnostics`、events、describe、Helm history | all workload Ready、delivery pass |
| Prometheus target down | Pod annotations、namespace RBAC、target page | API/worker/mock sink target UP |
| Scanner fails | tool version、network/database status、exit code | all configured scans complete with policy-compliant result |

通常のfailureではlog、diff、configuration、existing codeの順に調べ、原因仮説に対応する最小修正後に同じgateを再実行する。Secret値、payload、credentialをdiagnosticsへ保存しない。

## Cleanup and hygiene

実行したtopologyに対応するcleanupだけを使う。

```bash
make compose-down
make observability-down
make cluster-down
git status --short --branch
```

`make observability-down`と`make cluster-down`は同じproject cluster cleanupを行うため、clusterが存在する場合に一方を選ぶ。Unrelated container、network、volume、clusterへ削除commandを適用しない。Cleanup後はHooklane project resource、dedicated kubeconfig、test Redis container、temporary diagnosticsが残っていないことを確認する。
