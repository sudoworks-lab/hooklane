# Hooklane Goal Loop 計画

## 位置付け

- 正本は人間管理の `docs/GOAL.md` とする。この計画はGOALの実装順、受け入れ基準、検証手順を具体化するが、仕様を追加しない。
- 1回のGoal Loopでは、マイルストーン順で未完了かつ未blockedのfeatureを1件だけ扱う。実装、feature固有検証、`docs/features.json`更新、必要な公開evidence更新、local commitまでを同じ周で完了する。
- KICKOFFでは計画、feature、初期状態、段階的な環境確認だけを生成する。アプリケーション、container、Kubernetes、observability、CI、incident drillの実装は人間レビュー後に開始する。
- KICKOFF後は、このファイルの既存本文を変更せず、必要な判断だけを末尾の「判断メモ」へ追記する。

## 想定アーキテクチャ

```text
client
  | POST /v1/events, GET /v1/events/{event_id}
  v
FastAPI API ---- event status / idempotency ---- Redis
  |                                             | Redis Streams consumer group
  +---------------- enqueue --------------------+
                                                v
                                         worker replicas
                                                |
                                      fixed internal HTTP target
                                                v
                                            mock sink

API / worker / Redis / mock sink
  | structured logs + /metrics
  v
Prometheus ---- alert rules ---- Runbooks
  |
Grafana dashboards
```

### 責務境界

- `API`: request validation、event ID採番、idempotency判定、enqueue、状態参照、API用health endpoint、metricsを担当する。
- `worker`: consumer group、配送、attempt更新、retry scheduling、dead-letter、pending message回収、worker用health、graceful shutdownを担当する。
- `mock sink`: 正常応答、5xx、遅延などを決定的に再現し、event ID単位の受信結果だけを検証可能にする。任意URL配送は提供しない。
- `Redis`: Streams、consumer group、event status、idempotency mapping、retry schedule、dead-letterを単一instanceで保持する。Redis高可用性は対象外とする。
- `observability`: payloadを収集せず、event IDまたはrequest IDでlogを相関し、低cardinalityのPrometheus metricsを公開する。
- `delivery contract`: at-least-onceとし、成功時だけackする。重複配送は起こり得るため、downstreamはevent IDで重複排除する。

### 想定repository構造

```text
src/hooklane/{api,worker,mock_sink,domain,queue,delivery,observability}/
tests/{unit,integration,e2e,incidents}/
deploy/{compose,kind,helm}/
observability/{prometheus,grafana}/
scripts/
docs/{adr,runbooks,incidents}/
.github/workflows/
```

実際のdirectory名はF001でMake targetとimport boundaryを同時に検証して固定する。別repository、Git submodule、外部runtime repositoryは作らない。

## 技術選定と理由

| 領域 | 選定 | 理由と固定時期 |
|---|---|---|
| Application | Python、FastAPI、Pydantic | GOALのハード制約。利用可能なPythonは現時点で`python3 3.12.3`。projectの対応minor versionとdependency exact versionはF001で互換性を確認して固定する。 |
| Redis client | Python用async Redis client | FastAPIとworkerで接続管理を共有し、Streams、consumer group、status、idempotencyを同じboundaryで扱う。packageとversionはF001で固定する。 |
| Queue | Redis Streams consumer group | at-least-once、pending inspection、別consumerによるclaimを提供し、GOALの配送保証を直接検証できる。 |
| Retry | Redis上の永続retry scheduleとworker scheduler | process内sleepだけにせず、worker再起動後も予定を保持する。具体的なkey schemaとatomicityはF007のtestとADRで固定する。 |
| HTTP client | timeoutを明示できるasync client | downstream error分類、connection timeout、graceful cancellationをtest可能にする。packageとversionはF001で固定する。 |
| Test | pytest系、HTTP test client、実Redisを使うintegration test | unitとintegrationの境界を分け、Redis Streamsの実挙動はmockで代替しない。exact dependencyはF001で固定する。 |
| Static quality | Ruff相当のlint、mypy相当のtype check | Pythonの品質gateを単一Make interfaceで再現する。採用commandとversionはF001で固定する。 |
| Local runtime | Docker Compose | API、worker、Redis、mock sinkを同じnetworkで再現し、local E2Eの入口を統一する。 |
| Kubernetes | kind、Helm | GOALのハード制約。node image、chart API compatibility、tool versionはF001/F013で固定し、`latest`を使わない。 |
| Metrics | Prometheus client、Prometheus、Grafana | GOALのハード制約。metric名とlabel contractはF016でtestに固定し、dashboard、alert、SLO、Runbookから参照する。 |
| Security gate | Gitleaks、OSV-Scanner、Trivy相当、Helm/schema validator | secret、dependency、image、manifestを別々の機械判定で検査する。未導入toolの取得は人間承認後とし、versionとfail policyはF020-F021で固定する。 |
| Automation | Makefileと薄いPython/shell entrypoint | 利用者の主要commandをMake targetへ統一する。JSON、API、state、retryなどの本体logicはPythonに置き、shellは起動と検証の薄いglueに限定する。 |
| CI | GitHub Actions | GOALの成果物。localと同じMake targetを呼び、最小permissions、未信頼PRでsecret不使用、action commit SHA固定をF021で検証し、kind E2E jobはF022で有効化する。pushとActions実行は人間判断まで行わない。 |

dependency lock方式、base image、kind node image、tool exact versionはKICKOFFで推測固定しない。F001で導入環境、upstream support、再現可能性を確認し、lockfileとADRへ記録する。

## マイルストーン

各マイルストーンは対応feature 1件だけを完了させる単位とする。記載commandは、そのマイルストーンの実装で同時に追加または有効化し、featureの`steps`と合わせてすべて実行する。29件は当初の20件目安を超えるが、1周で実装、検証、`features.json`更新、必要な公開evidence更新、local commitまで完了できる粒度を優先し、container/Compose、chart/deploy、observability/alert、security/CI、kind E2E/rollout、各incident、docs/clean-roomを分離した。内訳はfunctional 13件、quality 14件、docs 2件である。

### M01 Repository skeletonとtoolchain

- 対応feature: `F001`
- Scope: Python package/testの責務別skeleton、`pyproject.toml`、dependency lockfile、Makefile、`doctor`、段階的`smoke-fast`を追加する。アプリ機能は実装しない。
- 受け入れ基準: exact versionを固定可能で`latest`がなく、`doctor`がtool、Docker接続、resource前提をsecret/env値なしで判定し、`scripts/init.sh`が追加targetを実行する。
- 検証command: `bash scripts/init.sh`; `make doctor`; `make smoke-fast`

### M02 Event modelとAPI受付

- 対応feature: `F002`
- Scope: event domain model、request/response schema、`POST /v1/events`の202 contract、一意なevent ID、初期状態を実装する。Redis enqueueはM03で扱う。
- 受け入れ基準: valid JSONが202を返し、不正requestは定義済み4xxとなり、Idempotency-Keyなしの別requestは別event IDになる。
- 検証command: `make test-unit TESTS='tests/unit/test_event_model.py tests/unit/test_event_api.py'`

### M03 Redis enqueueと初期状態

- 対応feature: `F003`
- Scope: Redis接続boundary、Streams enqueue、status recordのatomicity、Redis error responseを実装する。
- 受け入れ基準: 202 responseのevent IDでstreamとstatusを関連付けられ、Redis失敗を成功として返さず、sensitive valueをlogへ出さない。
- 検証command: `make test-integration TESTS='tests/integration/test_enqueue.py'`

### M04 Worker正常配送

- 対応feature: `F004`
- Scope: consumer group、worker、allowlist固定のmock sink、成功時ack、delivery statusを実装する。
- 受け入れ基準: 正常配送だけがackされ、sink失敗はpendingに残り、at-least-once contractをtestで確認できる。
- 検証command: `make test-integration TESTS='tests/integration/test_worker_delivery.py tests/integration/test_mock_sink.py'`

### M05 状態参照API

- 対応feature: `F005`
- Scope: `GET /v1/events/{event_id}`とstate transitionのread modelを実装する。
- 受け入れ基準: 全配送状態とattempt countを取得でき、未知IDは404となり、内部Redis表現を露出しない。
- 検証command: `make test-integration TESTS='tests/integration/test_event_status.py'`

### M06 Idempotency

- 対応feature: `F006`
- Scope: Idempotency-Key、canonical request fingerprint、atomicなcreate/reuse/conflict判定を実装する。
- 受け入れ基準: 同一key/内容は同一eventかつ1 enqueue、同一key/異内容は409、並行requestでも二重登録しない。
- 検証command: `make test-integration TESTS='tests/integration/test_idempotency.py'`

### M07 Retry分類、backoff、jitter

- 対応feature: `F007`
- Scope: retryable/non-retryable分類、exponential backoff、bounded jitter、永続retry scheduleを実装する。
- 受け入れ基準: error classごとのpolicyがunit testで決定的に検証でき、scheduled eventが再配送される。
- 検証command: `make test-unit TESTS='tests/unit/test_retry_policy.py'`; `make test-integration TESTS='tests/integration/test_retry_delivery.py'`

### M08 Dead-letterとattempt上限

- 対応feature: `F008`
- Scope: attempt上限、非retry失敗、dead-letter stream、最終error分類を実装する。
- 受け入れ基準: 対象eventが一度だけdead-letterへ移り、status/attemptが一致し、元messageがackされる。
- 検証command: `make test-unit TESTS='tests/unit/test_dead_letter_policy.py'`; `make test-integration TESTS='tests/integration/test_dead_letter.py'`

### M09 Pending message回収

- 対応feature: `F009`
- Scope: pending inspection、idle threshold、別consumerのclaim、crash recoveryを実装する。
- 受け入れ基準: 配送中worker停止後に別workerが回収し、eventを最終状態へ進め、stream/status/sink receipt上のデータ消失がない。
- 検証command: `make test-integration TESTS='tests/integration/test_pending_recovery.py'`

### M10 Health semanticsとgraceful shutdown

- 対応feature: `F010`
- Scope: API/worker別startup、readiness、livenessとSIGTERM処理を実装する。
- 受け入れ基準: APIは初期化完了前、workerはconsumer groupとscheduler準備完了前にreadyにならず、startup失敗を成功扱いしない。livenessは外部依存の一時障害を直接条件にせず、shutdown中のmessageを失わない。
- 検証command: `make test-unit TESTS='tests/unit/test_health.py tests/unit/test_shutdown.py'`; `make test-integration TESTS='tests/integration/test_graceful_shutdown.py'`

### M11 Docker imageとbaseline hardening

- 対応feature: `F011`
- Scope: Dockerfile、`.dockerignore`、placeholder-only `.env.example`、image pin、全container共通hardening contractを追加する。Compose起動はM12で扱う。現行`.gitignore`競合は人間承認後に最小mergeする。
- 受け入れ基準: application imageを固定baseからbuildでき、全containerに適用するpolicyと理由付き例外を機械検査できる。`.env.example`は値なしで追跡可能になる。
- 検証command: `make images-build`; `make image-contract`; `make container-policy-check`; `make env-example-check`; `git check-ignore -q .env.example`がexit code 1; `git ls-files --error-unmatch .env.example`

### M12 Docker Compose local E2E

- 対応feature: `F012`
- Scope: API、worker、Redis、mock sinkのCompose、health check、local smoke/E2E、cleanupを追加する。
- 受け入れ基準: 4 serviceで正常配送、状態参照、idempotency、retry、dead-letter、pending回収を検証し、全serviceのhardeningまたは理由付き例外を確認後に後片付けできる。
- 検証command: `make compose-up`; `make smoke`; `make e2e-local`; `make container-policy-check TARGET=compose`; `make compose-down`

### M13 kind configとHelm skeleton

- 対応feature: `F013`
- Scope: fixed kind config、Helm chart skeleton、values/schema、4 workload templateを追加する。cluster作成とdeployは行わない。
- 受け入れ基準: kind config contract、helm lint、helm template、values schemaの基本検証がlocalで通り、検証targetがdeployしない。
- 検証command: `make kind-config-check`; `make chart-validate-base`

### M14 Helm deployと全Ready

- 対応feature: `F014`
- Scope: local image load、kind cluster作成、Helm install、API/worker/Redis/mock sinkのReady確認とbasic chart smokeを追加する。
- 受け入れ基準: 4 workloadがReadyで基本配送が通り、外部cloud/SaaS/実credentialなしでdeployとcleanupを実行できる。
- 検証command: `make cluster-up`; `make deploy`; `kubectl wait --for=condition=Ready pods --all --timeout=180s`; `make chart-smoke`; `make cluster-down`

### M15 Kubernetes resiliencyとsecurity

- 対応feature: `F015`
- Scope: replicas、resources、role別probes、securityContext、rolling strategy、PodDisruptionBudget (PDB)、graceful terminationと全container policyをchartへ追加する。
- 受け入れ基準: API/workerだけでなくRedis/mock sinkを含む全rendered containerがhardening原則を満たし、機能上の例外は理由と限定範囲を持つmachine-readable entryとして検出される。
- 検証command: `make chart-validate`; `make container-policy-check TARGET=helm`

### M16 Structured logsとapplication metrics

- 対応feature: `F016`
- Scope: JSON log、correlation ID、metrics endpoint、request/enqueue/queue/delivery/retry/dead-letter/Redis error metricsを実装する。
- 受け入れ基準: log相関とmetric contractをtestでき、payload/credential/secret/password/個人情報がlog、label、artifactへ入らない。
- 検証command: `make test-unit TESTS='tests/unit/test_logging.py tests/unit/test_metrics.py'`; `make test-integration TESTS='tests/integration/test_observability_contract.py'`

### M17 Prometheus、Grafana、SLI dashboard

- 対応feature: `F017`
- Scope: scrape設定、ServiceMonitor相当、Prometheus、Grafana provisioning、主要SLI dashboardを追加する。alertはM18で扱う。
- 受け入れ基準: target、query、SLI panelが確認でき、Prometheus/Grafanaを含む全追加imageがexact pinで`latest`なしとなり、hardening例外も機械検査される。
- 検証command: `make observability-up`; `make observability-smoke-base`; `make image-contract`; `make container-policy-check TARGET=observability`; `make observability-down`

### M18 Alert、Runbook、SLI/SLO link

- 対応feature: `F018`
- Scope: 最低4 alert、各Runbook、SLI/SLO/metric相互link、observability aggregate smokeを追加する。
- 受け入れ基準: delivery、retry、dead-letter、Redis/queueのalertが有効で、各alertから切り分け、暫定対応、復旧確認へ辿れる。
- 検証command: `make alert-rules-check`; `make observability-smoke`

### M19 lint、type check、unit/integration test

- 対応feature: `F019`
- Scope: source/test/support codeのlint/typecheck、unit/integration suite、`make verify`のbaseを完成させる。
- 受け入れ基準: 個別targetとF019時点の`make verify`が失敗を隠さず、unit/integration結果を区別できる。
- 検証command: `make lint`; `make typecheck`; `make test`; `make verify`

### M20 Security gate

- 対応feature: `F020`
- Scope: Gitleaks、OSV-Scanner、Trivy、fail policyを追加し、securityを`make verify`へ加える。
- 受け入れ基準: secret、dependency、全image scanがpolicyを満たし、secret finding 0件である。
- 検証command: `make security`; `make verify`

### M21 Helm/schema validationとbaseline CI

- 対応feature: `F021`
- Scope: full chart/schema validation、必須成果物`.github/workflows/ci.yml`のbaseline GitHub Actions、local `ci-contract`を追加し、chart validationを`make verify`へ加える。
- 受け入れ基準: local gateが通り、`.github/workflows/ci.yml`が存在して同じMake target、最小permissions、不変action参照を使う。kind E2E jobはまだpass条件にしない。
- 検証command: `make chart-validate`; `make verify`; `make ci-contract`

### M22 kind E2EとCI job

- 対応feature: `F022`
- Scope: kind上のnormal/retry/recovery/status E2Eと、それを呼ぶCI jobを有効化する。
- 受け入れ基準: E2Eが通り、CI contractが`make e2e-kind`との対応を確認し、clusterを後片付けできる。
- 検証command: `make cluster-up deploy`; `make e2e-kind`; `make ci-contract`; `make cluster-down`

### M23 Rolling updateとrollback

- 対応feature: `F023`
- Scope: availabilityを保つrolling updateと意図的なfailed releaseからのrollbackを自動化する。
- 受け入れ基準: update中のPod遷移を確認でき、failed releaseがReadyにならず、rollback後のsmokeが通る。
- 検証command: `make cluster-up deploy`; `make rollout-smoke`; `make cluster-down`

### M24 Downstream 5xx drill

- 対応feature: `F024`
- Scope: downstream 5xxの再現、signal、切り分け、暫定対応、復旧、データ消失判定を1 scenarioとして実装する。
- 受け入れ基準: 専用commandが独立して通り、retry後の復旧とstream/status/sink receiptの整合を確認できる。
- 検証command: `make incident-downstream-5xx`

### M25 Redis outage drill

- 対応feature: `F025`
- Scope: Redis停止/再開、health signal、受付/workerの挙動、暫定対応、復旧、データ消失判定を実装する。
- 受け入れ基準: 専用commandが独立して通り、確認可能な受付済みeventの整合を確認できる。
- 検証command: `make incident-redis-outage`

### M26 Worker stop drill

- 対応feature: `F026`
- Scope: 配送中worker停止、pending signal、別worker claim、復旧、重複可能性、データ消失判定を実装する。
- 受け入れ基準: 専用commandが独立して通り、at-least-onceを維持して最終状態へ進む。
- 検証command: `make incident-worker-stop`

### M27 Incident aggregateとpostmortem

- 対応feature: `F027`
- Scope: 3 drillのaggregate target、各incident record、blameless postmortem sampleを追加する。新scenarioは追加しない。
- 受け入れ基準: 3 commandの独立結果を集約し、記録とpostmortemがRunbookへ相互参照する。
- 検証command: `make incident-smoke`

### M28 Core documentation

- 対応feature: `F028`
- Scope: ARCHITECTURE、ADR、SLO、OPERATIONS、SECURITY、LIMITATIONSと`docs-core-check`を追加し、core docs checkを`make verify`へ加える。
- 受け入れ基準: 文書が実装、metric、alert、Runbook、incident、hardening例外と一致し、at-least-onceと制約を明記する。
- 検証command: `make docs-core-check`; `make verify`

### M29 README、DEMO、clean-room

- 対応feature: `F029`
- Scope: README、DEMO、full docs-check、final verify、local clean cloneでのsetup/deploy/demo/cleanupを完成させる。
- 受け入れ基準: 3分demoにarchitecture、主要failure mode、health semantics、verification results、constraintsが機械検出でき、clean-room後に禁止artifactを残さない。
- 検証command: `make docs-check`; `make verify`; `make clean-room`; `git status --short --branch`

### 検証interfaceの段階依存

- M19で`make verify`へlint、typecheck、unit/integration testを集約する。
- M20でsecurity、M21でchart/schema validation、M28でdocs-core-check、M29でfull docs-checkを順に追加する。未実装targetを前段featureのpass条件にしない。
- M21でbaseline CIを検証し、M22で`make e2e-kind`と対応CI jobを有効化して`make ci-contract`を再実行する。kind E2Eは重い独立targetであり、GOALの`make verify`集約対象には含めない。

## 毎周の基本検証

Goal Loop開始時は、feature固有実装へ入る前に次を実行する。

```bash
bash scripts/init.sh
git diff --check
```

`scripts/init.sh` は段階的に次を行う。

1. Bash、Git、Make、Docker、Compose、kubectl、kind、Helm、Gitleaks、OSV-Scanner、Trivyの存在とversionを確認する。未導入toolは理由を表示してskipし、optional toolのversion command失敗はexit code付きwarnとして扱う。
2. `python`を先に探し、なければ`python3`を使う。利用可能なら`docs/features.json`をJSON parserで検証し、存在する`pyproject.toml`と`src/`を検査する。
   `src/`のPython syntaxは各`.py`を`ast.parse`してread-onlyで確認し、`__pycache__`その他のgenerated fileを作らない。
3. `scripts/init.sh`と`scripts/loop.sh`のshell syntaxを検証する。
4. Makefileとtargetが追加済みなら`make doctor`と`make smoke-fast`を実行し、未追加なら理由を表示してskipする。
5. package install、外部download、service/tool起動、環境変数値の出力は行わない。依存導入やservice起動は、対応featureの明示Make targetを人間レビュー後に実行する。

基本検証が失敗した場合は新featureへ進まず、同じ周で原因を修理して再検証する。feature固有の重いE2E/security/incident commandは該当マイルストーンで実行し、基本検証へ無条件には含めない。

## GOAL要件追跡

### Goals

| GOAL要件 | 対応feature |
|---|---|
| API、worker、mock sink、Redis、Kubernetes、observability、tests、docsを単一repositoryで管理 | F001、F011-F029 |
| `POST /v1/events`の202、event ID、現在状態 | F002、F003 |
| `GET /v1/events/{event_id}`の配送状態、試行回数 | F005 |
| Idempotency-Keyの重複抑止と内容競合 | F006 |
| Redis Streams consumer groupとat-least-once mock sink配送 | F003、F004 |
| 成功時のみack、別workerのpending回収 | F004、F009 |
| retry分類、exponential backoff、jitter、dead-letter | F007、F008 |
| API/workerのstartup、readiness、liveness、graceful shutdown | F010、F015 |
| sensitive dataを記録しない相関可能なstructured logs | F016 |
| requestからRedis errorまでのPrometheus metrics | F016 |
| Composeで4 serviceと正常/失敗確認 | F011、F012 |
| kindへHelm deploy | F013、F014 |
| replicas、resources、probes、security、rolling update、PodDisruptionBudget (PDB)、termination | F015、F023 |
| Prometheus、Grafana、主要SLI、4 alerts、Runbook | F017、F018 |
| downstream 5xx、Redis停止、worker停止のdrill | F024-F027 |
| lint、typecheck、unit/integration、scan、chart、kind E2Eの統一interface | F019-F023 |
| README、Architecture、ADR、SLO、Runbook、Security、Limitations、Demo整合 | F018、F027-F029 |
| clean環境からsetup、verify、demo、cleanup | F029 |

### ハード制約

| 制約 | 計画上の担保 |
|---|---|
| 単一repositoryで開発する | F001とrepository構造contractで検査する。 |
| Git submoduleまたは別repositoryへのruntime依存なし | F001のrepository依存contractで検査する。 |
| Python/FastAPI、Redis | F001-F010で使用する。 |
| kind/Helm、Prometheus/Grafana | F013-F018で使用する。 |
| 導入環境確認後にexact version固定、`latest`禁止 | F001、F011、F013、F017、F020でlock/configを検査する。 |
| 外部cloud/SaaS/実credential不要、sinkはmock/allowlist限定 | F004、F011-F014、F029でcontractとclean-roomを検査する。 |
| at-least-once、重複可能性、event ID重複排除を明記 | F004、F028、F029でtestとdocsを検査する。 |
| livenessは外部依存の一時障害を直接条件にしない | F010、F015でtestする。 |
| payload、secret、credential、Redis password、個人情報をlog/fixture/artifact/commitへ含めない | F003、F006、F016、F020、F029でnegative assertionとscanを行う。 |
| non-root、privilege escalation禁止、capability drop、read-only rootfs、RuntimeDefault seccomp | F011、F012、F015、F017で全containerと理由付き例外を検査する。 |
| Actions最小permissions、未信頼PRでsecret不使用、action不変参照 | F021、F022の静的contractで検査する。 |
| 全主要操作を単一interfaceで実行 | F001、F011-F029でMake targetを増分追加する。 |
| production-ready、実SLO達成、実務経験と誤認させない | 人間レビュー項目で確認する。 |
| 7日P0のためNon-goalsを追加しない | 全featureを固定し、追加/description変更を禁止する。 |
| push、外部公開、破壊的操作は人間判断なしに行わない | GOAL、公開evidence、human review gateで明示する。 |

### 成果物

| 成果物群 | 対応feature |
|---|---|
| API、worker、mock sink、domain/queue/delivery/observability modules | F001-F005、F016 |
| Streams、retry schedule、dead-letter、status、idempotency | F003、F006-F009 |
| Dockerfile、Compose、`.dockerignore`、placeholder `.env.example` | F011、F012 |
| kind、Helm、values、Kubernetes resources | F013-F015 |
| Prometheus scrape/ServiceMonitor相当、alerts、Grafana dashboard | F016-F018 |
| unit、integration、kind E2E、incident drill test/script | F019、F022、F024-F027 |
| lint/type/test/security/chart/kind E2E CI workflow | F019-F022 |
| README、Architecture、SLO、Operations、Security、Limitations、Demo | F028、F029 |
| Runbooks、incident記録/postmortem、`docs/adr/`配下のADR | F018、F027-F029 |
| doctor、起動、検証、demo、診断、cleanup interface | F001、F011-F029 |

### Done when

| Done when | 対応feature |
|---|---|
| `make doctor` | F001 |
| `make lint`、`make typecheck` | F019 |
| `make test` | F019 |
| `make security`、secret finding 0 | F020 |
| `make compose-up`後の`make smoke` | F012 |
| `make e2e-local` | F012 |
| `make cluster-up`と`make deploy`、全workload Ready | F014 |
| `make e2e-kind` | F022 |
| `make rollout-smoke` | F023 |
| `make observability-smoke` | F018 |
| `make incident-smoke` | F027 |
| `make docs-check` | F029 |
| `make verify`の最終集約gate | F019-F021、F028、F029 |
| CIがlocalと同じMake targetを定義 | F021、F022 |
| 一時directoryのlocal cloneで再現 | F029 |
| demo後に禁止artifactが残らない | F029 |
| README/Limitationsに配送保証、単一構成、測定範囲、未実装を明記 | F028、F029 |
| SLI/SLO、alert、Runbook、drill、metric名が相互参照 | F016-F018、F024-F029 |
| 3分以内のarchitecture/failure/health/verification/constraint demo | F029 |
| 誇張・誤認がないことの人間最終レビュー | 人間レビュー項目 |

## v0.1 public snapshot

- F001〜F029の最終状態は`docs/features.json`、公開可能なacceptance summaryは`docs/RELEASE_EVIDENCE.md`を正本とする。
- 内部execution ledgerと内部Git historyはpublic snapshotへ含めない。
- Hooklane sourceはMIT Licenseで提供し、third-party softwareの確認境界は`THIRD_PARTY_NOTICES.md`に記録する。
- Distributionはsource-onlyで、prebuilt container image、registry artifact、release archive、binaryを提供しない。
- GitHub hosted Actions、cloud production、external downstream、長時間load、本番trafficは未確認のままである。

## 仮定

- 新規projectとして`hooklane`を開始し、既存application sourceとの互換性維持は不要とする。
- runtime serviceはlocal containerまたはkind内で完結し、単一Redis instanceと単一kind clusterで検証する。
- GitHubへpushしなくても、CIと同じMake targetをlocalで検証できる構成にする。
- P0のvertical slice、再現性、障害対応、docs整合を優先し、feature追加より固定した29件の完了を優先する。
- metric名、Redis key schema、timeout/retry既定値は該当featureのtestとADRで固定し、KICKOFFでは推測で仕様化しない。
- 同一worktreeのwriterは常に1体とし、1周につきfeature 1件だけを扱う。

## Non-goalsの再確認

次はこの計画へ含めず、P0中に追加しない。

- EKS、GKE、AKSその他cloud deploy、Terraform、Argo CD、Flux、service mesh、multi-cluster。
- authentication、管理画面、multi-tenant、課金、外部公開API、利用者指定の任意配送URL。
- exactly-once保証、Redis Cluster、Sentinel、managed Redis、multi-AZ。
- OpenTelemetry traces、Tempo、distributed tracing。
- HPA、NetworkPolicy、SBOM、provenance、GHCR releaseの必須化。
- `repo-health-doctor`統合、Kubernetes資格対策または機能網羅。
- 外部service secret、credential、個人情報、業務dataの利用。
- 自動push、外部公開、cloud課金。

KICKOFF自体ではDockerfile、Compose、Kubernetes manifest、Helm chart、Prometheus/Grafana、GitHub Actions、incident drillを作成せず、Goal Loop第1周も開始しない。

## 判断メモ

- 2026-07-13: 「v0.1 public snapshot」節のGitHub Actionsに関する記述は、公開前の計画時点を示す。現在の事実は[検証根拠](RELEASE_EVIDENCE.md)を正本とし、GitHub hosted Actionsでquality / security / chart gatesとkind delivery and recovery E2Eの成功を確認済み。
- v0.1.1のtagがcurrent source baseline。GitHub Releaseの有無はこのsource contractでは主張せず、cloud production、実在する外部downstream、multi-node／multi-zone、long-running load、本番traffic、30日SLO達成実績は未確認のままとする。
- `platform/aws-interview-v1`では、v0.1.1 runtime contractを基礎にTerraform AWS foundationを追加する。これはv0.1 local Goalの受入結果を置き換えず、AWS apply、ECR push、cloud runtimeの証明は別のhuman gateとする。
