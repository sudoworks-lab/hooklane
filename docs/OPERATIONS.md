# Hooklane 運用

## 対象範囲と前提

この文書はDocker Composeとproject専用kind clusterでHooklaneを起動、確認、復旧、cleanupする手順をまとめる。固定tool versionとresource条件は[`toolchain.toml`](../toolchain.toml)を正本とする。設計上の制約は[制約](LIMITATIONS.md)を参照する。

初期化と環境診断はrepository rootで実行する。

```bash
bash scripts/init.sh
make doctor
```

`scripts/init.sh`はproject-local virtual environmentとlock済みPython dependencyを準備する。`make doctor`は必要tool、Docker daemon、resourceを検査し、環境を変更しない。

## Make interface

| target | 用途 | 前提とcleanup |
|---|---|---|
| `make doctor` | local toolとresourceの事前確認 | cleanup不要 |
| `make images-build` | API、worker、mock sink imageをlocal build | local imageを作成 |
| `make compose-up` | Compose serviceをbuildしてReadyまで待機 | 終了時に`make compose-down` |
| `make smoke` | health、202受付、delivered statusを確認 | Compose起動済み |
| `make e2e-local` | idempotency、retry、dead-letter、pending recoveryを確認 | Compose起動済み、終了後に`make compose-down` |
| `make verify` | quality、security、chart、docs gateを集約 | image buildとscanner DB accessを含む |
| `make security` | Gitleaks、OSV-Scanner、Trivy filesystem／image scan | local scannerとDocker daemon |
| `make chart-validate` | Helm、values schema、Kubernetes policyを検証 | HelmとKubeconform |
| `make cluster-up` | project専用kind clusterを作成 | 終了時に`make cluster-down` |
| `make deploy` | local imageをloadしHelm releaseをinstall／upgrade | cluster起動済み |
| `make chart-smoke` | Helm testとchart runtime contractを確認 | deploy済み |
| `make diagnostics` | sanitized workload、event、statusを確認 | cluster起動済み |
| `make observability-up` | Hooklane、Prometheus、Grafanaをdeploy | 終了時に`make observability-down` |
| `make observability-smoke` | target、metric、dashboard、alert、復旧を確認 | observability起動済み |
| `make e2e-kind` | 正常配送、idempotency、retry、pending recoveryを総合検証 | 作成したclusterだけをcleanup |
| `make rollout-smoke` | rolling update、worker drain、bad release、rollbackを確認 | deploy済みcluster、終了時に`make cluster-down` |
| `make incident-smoke` | downstream、Redis、workerの3 incident drillを実行 | 作成したclusterだけをcleanup |

targetの正本は[Makefile](../Makefile)。long-running targetは失敗を成功扱いせず、sanitized diagnosticsを残す場合がある。

## Docker Composeの流れ

```bash
make compose-up
make smoke
make e2e-local
make compose-down
```

API、worker、mock sink、Redisがhealthyになり、smoke eventが`202 Accepted`後に`delivered`となることを確認する。`make compose-down`はHooklane Compose projectのcontainer、network、volumeだけを削除する。

Compose APIはloopbackの`127.0.0.1:18080`へ公開される。health、status、metricsの責務は分離されている。

```bash
curl --fail http://127.0.0.1:18080/health/live
curl --fail http://127.0.0.1:18080/health/ready
curl --fail http://127.0.0.1:18080/metrics
```

event requestは[`scripts/local_e2e.py`](../scripts/local_e2e.py)がpayloadをstdoutへ出さずに作成する。手動payloadの複製より`make smoke`を正本とする。

## quality、security、chart gate

```bash
make lint
make typecheck
make test
make security
make chart-validate
make docs-check
make verify
```

`make verify`は個別gateを順に実行し、いずれかがnon-zeroなら終了もnon-zeroとなる。scanner unavailable、database取得失敗、timeout、parse failureをfinding 0として扱わない。scanner設定は[`security-policy.json`](../security-policy.json)、container例外は[`container-policy.json`](../container-policy.json)を参照する。

## kindとHelmの基本操作

```bash
make cluster-up
make deploy
make chart-smoke
make cluster-down
```

API 2 replica、worker、mock sink、RedisがReadyとなり、Helm testが202受付と配送を確認する。`make deploy`はlocal image buildとkind loadを使い、external registryへpushしない。失敗時はcleanup前に`make diagnostics`を実行する。

project識別子とdedicated kubeconfigは[`scripts/kind_runtime.py`](../scripts/kind_runtime.py)と[`deploy/kind/cluster.yaml`](../deploy/kind/cluster.yaml)を正本とする。`make cluster-down`はHooklane専用clusterとdedicated kubeconfigだけを削除する。

## observabilityの操作

```bash
make observability-up
make observability-smoke
make observability-down
```

全targetのUP、event deliveryによるmetric増加、queue depthとpendingの0復帰、dashboard provisioning、障害注入後のalert復旧を確認する。dashboardは[Hooklane SLI and Operations](../charts/hooklane/files/grafana/dashboards/hooklane-overview.json)、alertは[rule file](../charts/hooklane/files/prometheus/rules/hooklane-alerts.yml)を正本とする。

manual inspectionが必要な場合は、`scripts/kind_runtime.py`が管理するdedicated kubeconfigとcontextを明示し、cluster-local Serviceをloopbackへ一時的にport-forwardする。確認後はport-forward processを停止する。

## log、metric、alert、event status

application logはJSON 1行で、request IDまたはevent ID、attempt、outcome、reason codeを使って追跡する。payloadやcredentialを含むRedis streamのraw dumpは診断手順にしない。

```bash
make diagnostics
```

status APIは既知のevent IDについて`queued`、`delivering`、`retry_scheduled`、`delivered`、`dead_letter`とattempt countを返す。Composeでの確認には次を使う。

```bash
curl --fail http://127.0.0.1:18080/v1/events/<event-id>
```

dead-letterは`hooklane_dead_letter_total`、`HooklaneDeadLetterIncreasing`、既知eventのstatusを突き合わせる。recovery後は`hooklane_queue_depth`、`hooklane_pending_messages`、`hooklane_worker_in_flight`が0、該当alertがinactive、新規eventが`delivered`となることを確認する。dead-letter replayの管理interfaceは実装していないため、raw queueを直接変更しない。

## alertとRunbookの一覧

| alert | 主signal | Runbook |
|---|---|---|
| `HooklaneApiUnavailable` | API `up` / `hooklane_service_ready` / series absence | [API availability](runbooks/HooklaneApiUnavailable.md) |
| `HooklaneWorkerUnavailable` | worker `up` / `hooklane_service_ready` / series absence | [Worker availability](runbooks/HooklaneWorkerUnavailable.md) |
| `HooklaneApiHighErrorRate` | `hooklane_http_requests_total` | [API high error rate](runbooks/HooklaneApiHighErrorRate.md) |
| `HooklaneQueueBacklogGrowing` | `hooklane_queue_depth` | [Queue backlog](runbooks/HooklaneQueueBacklogGrowing.md) |
| `HooklaneOldestEventTooOld` | `hooklane_oldest_queued_event_age_seconds` | [Oldest event](runbooks/HooklaneOldestEventTooOld.md) |
| `HooklaneDeliveryFailureRateHigh` | `hooklane_delivery_outcomes_total` | [Delivery failure](runbooks/HooklaneDeliveryFailureRateHigh.md) |
| `HooklaneRetryRateHigh` | `hooklane_retry_scheduled_total` | [Retry rate](runbooks/HooklaneRetryRateHigh.md) |
| `HooklaneDeadLetterIncreasing` | `hooklane_dead_letter_total` | [Dead letter](runbooks/HooklaneDeadLetterIncreasing.md) |
| `HooklaneRedisOperationFailures` | `hooklane_redis_operation_failures_total` | [Redis operation failure](runbooks/HooklaneRedisOperationFailures.md) |

SLI、threshold、measurement windowは[SLO設計目標](SLO.md)を参照する。

## incidentとpostmortemの一覧

`make incident-smoke`は3 drillを順に実行し、正常復旧とcluster cleanupを確認する。

- [downstream 5xxの記録](incidents/downstream-5xx.md)
- [Redis outageの記録](incidents/redis-outage.md)
- [worker stopの記録](incidents/worker-stop.md)
- [worker stopのpostmortem](incidents/postmortem-worker-stop.md)

個別drillは`make incident-downstream-5xx`、`make incident-redis-outage`、`make incident-worker-stop`で再現できる。既存cluster上で実行した場合はclusterを所有しないため自動削除しない。終了時にfailure injectionが通常設定へ戻り、queue、pending、in-flightが0、alertがinactiveであることを確認してから`make cluster-down`を実行する。

## rolling updateとrollback

```bash
make cluster-up
make deploy
make rollout-smoke
make cluster-down
```

`make rollout-smoke`はAPIのrolling strategy、継続request、worker graceful shutdown、意図的なbad releaseのfailure検知、Helm rollback、復旧後deliveryを確認する。bad releaseとfailure injectionはfinally pathでも正常revisionへ戻す。failure時はsanitized diagnosticsを取得し、release history、Pod event、readinessを確認してからcleanupする。irreversible database migrationはこの検証範囲に含めない。

## よくある失敗と初期確認

| 症状 | 最初に確認すること | 復旧条件 |
|---|---|---|
| Docker commandが失敗 | `make doctor`、Docker daemon、disk／memory | doctorがpass |
| Compose serviceがhealthyにならない | `docker compose ps`、service log、`make compose-down`後の再作成 | 全service healthy、smoke pass |
| APIがreadyにならない | Redis Pod／connection、API log、`hooklane_service_ready` | readiness 200、新規delivery成功 |
| queueが増える | worker Ready、sink mode、pending／oldest metric、Queue Runbook | queue／pending／oldestが0 |
| 配送失敗 | sink status、reason code、retry／dead-letter metric | sink accept、新規event delivered、alert inactive |
| Helm validation失敗 | rendered manifest、values schema、Kubeconform output | `make chart-validate`がpass |
| Pod rolloutが停止 | `make diagnostics`、event、Helm history | 全workload Ready、delivery pass |
| Prometheus targetがdown | Pod annotation、namespace RBAC、target page | API／worker／mock sink targetがUP |
| scannerが失敗 | tool version、network／database status、exit code | 全scanがpolicyに適合 |

通常のfailureではlog、diff、configuration、existing codeの順に調べ、原因仮説に対応する最小修正後に同じgateを再実行する。secret値、payload、credentialをdiagnosticsへ保存しない。

## cleanupとhygiene

実行した構成に対応するcleanupだけを使う。

```bash
make compose-down
make observability-down
make cluster-down
make runtime-hygiene-check
git status --short --branch
```

`make observability-down`と`make cluster-down`は同じproject cluster cleanupを行うため、clusterが存在する場合に一方を選ぶ。無関係なcontainer、network、volume、clusterへ削除commandを適用しない。cleanup後はHooklane project resource、dedicated kubeconfig、test Redis container、temporary diagnosticsが残っていないことを確認する。
