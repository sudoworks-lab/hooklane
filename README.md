# Hooklane

Hooklaneは、HTTPで受け付けたeventをRedis Streamsへ安全にenqueueし、非同期workerがdownstreamへ配送するlocal demonstrationである。受付と配送を分離し、retry、dead-letter、pending recovery、status参照、structured logs、Prometheus metricsを一つの再現可能なrepositoryで検証する。

このprojectはnon-production-readyである。Cloud production、external downstream、multi-node availability、30日SLO実績を提供しない。

## Quick start

PrerequisitesはDocker daemon、Python、Makeである。固定tool versionとresource条件は[`toolchain.toml`](toolchain.toml)を参照する。Default demoはcredentialやsecretを要求しない。

```bash
make demo-smoke
```

このtargetはproject-local initialization、Compose image buildと起動、liveness/readiness、eventの`202 Accepted`、`delivered` status、metrics endpointを検証し、成功・失敗のどちらでもHooklane Compose resourceをcleanupする。Expected resultは最後にdemo passとcontainer、network、volume cleanupが表示されることである。

`.env.example`はempty placeholderだけを持つtracked contractであり、default demoでcopyや値設定は不要である。値はこのREADMEへ転載しない。Placeholder-only contractは`make env-example-check`で確認できる。

## Source distribution

このrepositoryはsource-only distributionであり、Hooklaneのsource code、Dockerfile、Helm chart、configuration、documentation、検証手順を公開する。Prebuilt container image、container registry、release artifact、binary distributionは現時点で提供しない。

利用者はDockerfileからapplication imageをlocal buildする。Python dependency、base image、Redis、Prometheus、Grafanaその他third-party softwareには各上流のlicenseとnoticeが適用される。対象とversion正本は[Third-party notices](THIRD_PARTY_NOTICES.md)を参照する。Source-only公開はproduction readiness、hosted service、運用保証を意味しない。

## Problem and approach

Webhookやevent deliveryでは、downstream failureをAPI受付へ直接結合すると、timeout、retry、duplicate、status追跡が曖昧になる。Hooklaneは次の境界を置く。

1. APIがinputとidempotencyを検証する。
2. Eventと初期statusをRedisへ原子的に保存できた場合だけ202を返す。
3. WorkerがRedis Streams consumer groupから非同期配送する。
4. Retryable failureはbounded backoffとjitterで再試行し、policy上限またはnon-retryable failureはdead-letterへ終端する。
5. Worker停止後はpending messageをreplacement workerがclaimする。

詳細は[Architecture](docs/ARCHITECTURE.md)を参照する。

## Architecture overview

```text
client -> API -> Redis Streams -> worker -> downstream mock sink
           |          |
           +-> status +-> retry / pending / dead-letter

API / worker / mock sink -> Prometheus -> Grafana / alerts -> Runbooks
```

- APIはrequest validation、idempotency、enqueue、status、health、metricsを担当する。
- Redisはqueue、status、retry schedule、consumer pending、dead-letterを保持する。
- Workerはdelivery、retry分類、pending recovery、graceful shutdownを担当する。
- Mock sinkはlocal success receiptと安全なfailure injectionを提供する。
- Helm chartはkind上のapplication、Redis、任意のPrometheus/Grafanaを管理する。

## Key guarantees

- Redisへeventを永続化できた場合だけ`202 Accepted`を返す。
- 同じ`Idempotency-Key`と同じrequestは同じevent IDへ収束し、異なるcontentへのkey再利用は409となる。
- Deliveryはat-least-onceで、worker停止時もpending recoveryによりaccepted eventを処理継続する。
- Downstreamはevent IDをdeduplication keyとしてduplicate side effectを抑止できるcontractを持つ。
- Liveness、readiness、metrics endpointを分離し、Redis outage時はAPI readinessをfalseにしてfalse enqueueを防ぐ。
- Structured JSON logsはpayload、`Idempotency-Key`生値、credential、connection secretを含めない。
- Metrics labelは有限集合に限定し、event ID、request ID、raw URL、user inputを使わない。
- Local quality、security、Helm、kind、rollout、observability、incident contractはMake targetから再現できる。

## Non-guarantees

- Exactly-once deliveryではない。Side effect後・ack前の停止では同じevent IDを再配送し得る。
- Downstream側のevent-ID deduplication実装は利用側の責務である。
- Single Redisとsingle-node kindにHAやautomatic failoverはない。
- Authentication、multi-tenant authorization、NetworkPolicy、autoscaling、distributed tracingは実装していない。
- External downstream、long-running load、multi-zone、production trafficは未検証である。
- Alertmanager notification destinationとon-callは構築していない。

全項目は[Limitations](docs/LIMITATIONS.md)を正本とする。

## Local Compose demo

Serviceを起動したまま個別contractを確認する場合は次を使う。

```bash
bash scripts/init.sh
make compose-up
make smoke
make e2e-local
make compose-down
```

`make smoke`は202受付とdelivered status、`make e2e-local`はidempotency、retry、dead-letter、pending recoveryを確認する。途中で失敗した場合も`make compose-down`でHooklane Compose projectだけをcleanupする。Event payloadはtest outputへ表示しない。

## kind and Helm demo

正常配送、idempotency、retry、worker pending recovery、status API、Prometheus targetを一度に検証する。

```bash
make e2e-kind
```

Targetはproject専用clusterがなければ作成し、local imageをbuild/loadし、Helm releaseとobservabilityをdeployする。Helm testとE2E完了後、自分で作成したclusterだけをcleanupする。

Application chartだけを段階確認する場合は次を使う。

```bash
make cluster-up
make deploy
make chart-smoke
make cluster-down
```

## Observability

Prometheus、Grafana、application scrape、SLI dashboard、alert ruleを検証する。

```bash
make observability-up
make observability-smoke
make observability-down
```

DashboardにはAPI rate/error/latency、enqueue、queue depth、oldest age、delivery、retry、dead-letter、pending、Redis error、API replicas、worker in-flightを表示する。SLI targetとPromQLは[SLO](docs/SLO.md)、alert対応は[Operations](docs/OPERATIONS.md#alert-and-runbook-index)を参照する。OpenTelemetry tracesとdistributed tracingは対象外である。

## Rolling update and rollback

API rolling strategy、継続request、worker graceful shutdown、intentional bad release、Helm rollback、復旧後deliveryを再現する。

```bash
make cluster-up
make deploy
make rollout-smoke
make cluster-down
```

Bad releaseはlocal cluster内だけで注入し、normal revisionへ復旧してからcleanupする。Irreversible database migrationは対象外である。

## Incident drills

Downstream 5xx、Redis outage、worker stopを順に注入し、metrics、alerts、structured logs、Runbooks、recovery、accepted event lossを確認する。

```bash
make incident-smoke
```

Aggregateはclusterがなければ作成し、自分で作成したclusterだけをcleanupする。各receiptとblameless postmortemは[incident index](docs/OPERATIONS.md#incident-and-postmortem-index)にある。

## Quality and security

```bash
make lint
make typecheck
make test
make security
make chart-validate
make docs-check
make verify
```

`make verify`はsyntax/config、Ruff、strict mypy、unit/integration、Gitleaks、OSV-Scanner、Trivy filesystem/image、Helm/schema/Kubernetes policy、documentation contractをfail-closedで集約する。Scanner findingを自動ignoreせず、tool failureをfinding 0として扱わない。Policyと残存riskは[Security](docs/SECURITY.md)を参照する。

## Clean-room verification

Full local verificationは重い処理で、Docker image build、scanner、複数のkind smokeとincident drillを含む。実行時間はhost resourceとimage/cache状態で変わる。

```bash
make clean-room
```

Targetはlocal HEADをhardlinkなしでtemporary cloneする。Commit前のfinal candidateを検証する場合は、明示stage済み変更だけをcloneへ適用し、unstaged/untracked fileがあればfailする。Sourceの`.venv`、cache、secret設定、untracked artifactをcopyせず、temporary clone内でinit、verify、demo、kind E2E、rollout、observability、incident smokeを実行し、runtimeとtemporary directoryをcleanupする。Remoteへpushまたはcloneしない。

## CI

[`ci.yml`](.github/workflows/ci.yml)はpull requestとmain branchでlocalの`make verify`を実行し、その成功後に`make e2e-kind`を独立jobで実行する。Repository permissionはread-only、third-party actionsはfull commit SHA pin、secret参照と`pull_request_target`はない。Image push、release、deployは行わない。

Workflowはlocal static contractで検証済みだが、GitHub hosted Actions上の実行は未確認である。Remote repository情報が確定していないためCI badgeは掲載しない。

## Goal Loop development runner

Repository内のGoal Loopを再開する場合は、外部runnerが`docs/features.json`から未完了featureを1件だけ選び、そのfeature専用の新しいagent processを起動する。

```bash
bash scripts/loop.sh codex 1 --status
bash scripts/loop.sh codex 1 --dry-run
bash scripts/loop.sh codex 1 --iteration-timeout 1800
```

各agent processは1 featureだけを扱い、次のfeatureは外部runnerが別processで開始する。Feature別attempt上限、wall-clock timeout、timeout receipt、process group終了をrunnerが管理する。Write iterationはmain agentだけで実行し、subagent、agent delegation、`/goal`は既定で使わない。Python 3がない環境では安全性の低いshell fallbackへ縮退せずfail-closedする。Runner契約は`tests/test_loop_runner.py`と`tests/test_goal_loop_safety.py`で検証する。

## Documentation

- [Architecture and data flow](docs/ARCHITECTURE.md)
- [Operations and Runbook index](docs/OPERATIONS.md)
- [Security model](docs/SECURITY.md)
- [Limitations](docs/LIMITATIONS.md)
- [SLI / SLO design targets](docs/SLO.md)
- [Reproducible demonstration](docs/DEMO.md)
- [ADRs](docs/adr/0001-redis-streams-at-least-once.md)
- [Incident records and postmortem](docs/incidents/postmortem-worker-stop.md)
- [Release evidence](docs/RELEASE_EVIDENCE.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Cleanup

実行したtopologyに対応するproject-specific targetだけを使う。

```bash
make compose-down
make observability-down
make cluster-down
make runtime-hygiene-check
```

Unrelated container、network、volume、clusterを削除しない。Failure diagnosticsが作られた場合は内容にpayloadやcredentialがないことを確認し、今回生成したartifactだけをcleanupする。

## Project status

Planned local feature contractは[`docs/features.json`](docs/features.json)、公開可能なtechnical acceptance summaryは[Release evidence](docs/RELEASE_EVIDENCE.md)を正本とする。Implementation、tests、security gates、Helm/kind、observability、rollout、incident drills、core documentation、clean-room verificationを同じrepositoryで管理する。

実証済みなのはlocal mechanical gatesとproject-specific runtime smokeである。GitHub hosted Actions、cloud deployment、external downstream、本番traffic、30日SLO attainmentは未確認である。

## License and third-party notices

Hooklaneのsourceは[MIT License](LICENSE)で提供する。Copyright noticeは`Copyright (c) 2026 Hooklane contributors`である。

Third-party softwareの範囲と確認方法は[Third-party notices](THIRD_PARTY_NOTICES.md)に記録する。Python dependencyは`requirements.lock`、container/tool imageとversionは`container-policy.json`、`security-policy.json`、`toolchain.toml`を正本とする。Third-party sourceまたはbinaryはvendoredせず、generated lock fileとprovisioned dashboard/ruleはsource controlしている。Prebuilt imageは配布しない。
