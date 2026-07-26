# Hooklane

Hooklaneは、Webhookを受け付けてRedis Streamsへ保存し、非同期workerで配送するWebhook配送基盤。受付と配送を分離し、downstream障害をAPI受付の成否から切り離す。

- API、Redis Streams、worker、mock sink（既定）で構成する。配送先は`HOOKLANE_DOWNSTREAM_URL`でcontrolled endpointへ切り替えられる
- retry、dead-letter、pending recovery、配送status参照を持つ
- ローカル検証とGitHub Actionsでquality、security、Kubernetes contractを確認する
- cloud production、実在する外部downstream、長期負荷の実績は持たない

## 主な機能

- `POST /v1/events`でeventを受け付け、Redisへの永続化成功時だけ`202 Accepted`を返す
- `Idempotency-Key`で重複受付を抑止し、同一keyの内容不一致は`409 Conflict`とする
- Redis Streams consumer groupで非同期配送し、retry、dead-letter、pending recoveryを扱う
- `GET /v1/events/{event_id}`で配送状態とattempt数を参照する
- structured logs、Prometheus metrics、Grafana dashboard、alert、Runbookを同じrepositoryで管理する

## アーキテクチャ概要

```text
client -> API -> Redis Streams -> worker -> mock sink
           |          |
           +-> status +-> retry / pending / dead-letter

API / worker / mock sink -> Prometheus -> Grafana / alerts -> Runbooks
```

- APIは入力検証、idempotency、enqueue、status、health、metricsを担当する
- Redisはqueue、status、retry schedule、pending、dead-letterを保持する
- workerは配送、retry判定、pending recovery、graceful shutdownを担当する
- mock sinkは配送確認と障害注入にだけ使う

詳細なdata flowと責務は[アーキテクチャ](docs/ARCHITECTURE.md)を参照する。

## 障害時の動作

- downstreamのHTTP 5xx、timeout、connection failureはbounded backoffとjitterでretryする
- retry上限またはnon-retryable failureはdead-letterへ移す
- workerがside effect後・ack前に停止すると、pending messageを別workerがclaimする
- 配送はat-least-once。downstreamはevent IDを重複排除キーとして扱う必要がある
- Redisへ永続化できない場合、APIは`202 Accepted`を返さずreadinessをfalseにする

## 検証済みの範囲

- quality、security、Helm／Kubernetes、文書contractは`make verify`で機械検証する
- Compose、kind delivery and recovery E2E、rolling update／rollback、observability、incident drillを再現する
- GitHub hosted Actionsではquality / security / chart gatesとkind delivery and recovery E2Eの成功を確認済み
- v0.1.1のtagがcurrent source baseline。GitHub Releaseの有無はこのsource contractでは主張しない

これはcloud production、実在する外部downstream、multi-node／multi-zone、長時間負荷、本番traffic、30日SLO達成の実績ではない。制約の正本は[制約](docs/LIMITATIONS.md)に置く。

## Quick start

前提はDocker daemon、Python、Make。固定tool versionとresource条件は[toolchain.toml](toolchain.toml)を参照する。

```bash
bash scripts/init.sh
make demo-smoke
```

`make demo-smoke`はCompose image build、service health、event受付、非同期配送、status、metrics、project専用resourceのcleanupを確認する。payloadやcredentialを出力しない。

## 設計上の保証

- Redisへeventと初期statusを永続化できた場合だけ`202 Accepted`を返す
- 同じ`Idempotency-Key`と同じrequestは同じevent IDへ収束する
- 成功時だけackし、未ack messageはpending recoveryの対象になる
- payload、`Idempotency-Key`生値、credentialをlogやmetric labelへ出さない
- `HOOKLANE_REDIS_URL`は`redis://`または`rediss://`を受け、credential-bearing URLはSecretから注入する。ConfigMapへRedis URLを置かない
- metrics labelを有限集合に限定し、event IDやraw URLを使わない

## 保証しないこと・制約

- Exactly-once deliveryは提供しない
- Redis HA、automatic failover、backup、restoreは提供しない
- authentication、tenant isolation、NetworkPolicy、autoscaling、distributed tracingは実装しない
- Alertmanager notification destination、on-call、production Alertmanagerは構築していない

制約の一覧と証拠の解釈は[制約](docs/LIMITATIONS.md)を参照する。

## Docker Compose

```bash
make compose-up
make smoke
make e2e-local
make compose-down
```

`make smoke`は受付と配送、`make e2e-local`はidempotency、retry、dead-letter、pending recoveryを確認する。`make compose-down`はHooklane Compose projectのcontainer、network、volumeだけを削除する。

## kind / Helm

```bash
make cluster-up
make deploy
make chart-smoke
make cluster-down
```

`make deploy`はlocal image buildとkind loadを使い、external registryへpushしない。`make e2e-kind`は正常配送、idempotency、retry、pending recovery、status参照、cleanupをまとめて確認する。

## 監視

```bash
make observability-up
make observability-smoke
make observability-down
```

Prometheus target、application metrics、Grafana provisioning、SLI query、alert rule、障害後の復旧を確認する。metric、alert、Runbookの対応は[SLO](docs/SLO.md)と[運用](docs/OPERATIONS.md)を参照する。

## rolling update / rollback

```bash
make cluster-up
make deploy
make rollout-smoke
make cluster-down
```

`make rollout-smoke`はAPIのrolling update、worker drain、意図的なbad releaseの検知、Helm rollback、復旧後の配送を確認する。

## incident drill

```bash
make incident-smoke
```

downstream 5xx、Redis outage、worker stopを順に注入し、signal、Runbook、復旧、accepted eventの整合を確認する。個別の記録は[運用](docs/OPERATIONS.md#incidentとpostmortemの一覧)から辿れる。

## quality / security

```bash
make lint
make typecheck
make test
make security
make chart-validate
make docs-check
make verify
make clean-room
```

`make verify`はRuff、strict mypy、unit／integration test、Gitleaks、OSV-Scanner、Trivy、Helm／schema／Kubernetes、文書contractをfail-closedで集約する。`make clean-room`はtracked candidateから隔離した環境を作り、主要runtime検証とcleanupを再実行する。

## GitHub Actions

[ci.yml](.github/workflows/ci.yml)はpull requestとmainで`make verify`を実行し、その成功後に`make e2e-kind`を実行する。quality / security / chart gates、kind delivery and recovery E2E、cleanupはGitHub hosted Actionsで成功済み。成功時のfailure diagnostics uploadはskipとなる。

workflowはread-only permission、full commit SHAで固定したaction、secretを要求しないtriggerを使う。Hosted CIの成功はcloud productionや本番trafficの実績を意味しない。

## 詳細文書

- [アーキテクチャ](docs/ARCHITECTURE.md)
- [運用とRunbook一覧](docs/OPERATIONS.md)
- [security boundary](docs/SECURITY.md)
- [制約](docs/LIMITATIONS.md)
- [SLI / SLO設計目標](docs/SLO.md)
- [再現手順](docs/DEMO.md)
- [検証根拠](docs/RELEASE_EVIDENCE.md)
- [開発と検証方法](docs/DEVELOPMENT.md)
- [v0.1.1 release notes](docs/releases/v0.1.1.md)
- [ADR](docs/adr/0001-redis-streams-at-least-once.md)

## cleanup

実行した構成に対応するtargetだけを使う。

```bash
make compose-down
make observability-down
make cluster-down
make runtime-hygiene-check
```

無関係なcontainer、network、volume、clusterには触れない。

## 配布範囲

このrepositoryはsource-onlyで配布する。source code、Dockerfile、Helm chart、configuration、documentation、検証手順を含む。prebuilt container image、container registry、release artifact、binary distributionは配布しない。

application imageはDockerfileからlocal buildする。Python dependency、base image、Redis、Prometheus、Grafana、validation toolには各上流のlicenseとnoticeが適用される。

## License / third-party notices

Hooklaneのsourceは[MIT License](LICENSE)で提供する。第三者softwareの範囲、version、確認方法は[third-party notices](THIRD_PARTY_NOTICES.md)を参照する。Python dependencyは[requirements.lock](requirements.lock)、imageとtoolの固定値は`container-policy.json`、`security-policy.json`、`toolchain.toml`を正本とする。
