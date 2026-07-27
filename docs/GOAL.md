# GOAL.md — spec

## 目的

小規模なWebhook配送サービスを題材に、非同期処理、Kubernetesデプロイ、可観測性、障害対応、CI品質ゲートを一貫したシステムとして完成させる。

第三者がローカル環境で再現・検証でき、設計判断と制約を説明できる公開可能な品質を目指す。ただし、個人検証を本番運用実績またはproduction-readyであるかのようには表現しない。

## Goals

- 1つのリポジトリ内で、API、worker、mock sink、Redis、Kubernetes構成、可観測性、テスト、運用文書を一貫して管理できる。
- `POST /v1/events` がJSONイベントを受け付け、`202 Accepted`、一意なevent ID、現在状態を返せる。
- `GET /v1/events/{event_id}` で配送状態と試行回数を確認できる。
- `Idempotency-Key` により同一イベントの二重登録を抑止し、同一keyで内容が異なる要求を競合として扱える。
- Redis Streamsのconsumer groupを使い、workerが設定済みmock sinkへイベントをat-least-onceで配送できる。
- 配送成功時のみmessageをackし、worker停止時の未ack messageを別workerが回収できる。
- retry可能な失敗にexponential backoffとjitterを適用し、上限到達後または非retry対象をdead-letterへ移せる。
- APIとworkerが役割に応じたstartup、readiness、liveness判定とgraceful shutdownを備える。
- payload本文、credential、secretを記録せず、event IDまたはrequest IDで追跡可能な構造化ログを出力できる。
- request、enqueue、queue、delivery、retry、dead-letter、Redis errorをPrometheus metricsとして公開できる。
- Docker ComposeでAPI、worker、Redis、mock sinkを起動し、成功配送と失敗時挙動をローカル確認できる。
- kind上へHelmでAPI、worker、Redis、mock sinkをデプロイできる。
- Kubernetes構成にreplica、resource requests/limits、startup/readiness/liveness probes、securityContext、rolling update、PodDisruptionBudget、graceful terminationが定義される。
- PrometheusとGrafanaで主要SLIを確認でき、最低4件のalert ruleから対応Runbookへ辿れる。
- downstream 5xx、Redis停止、配送中worker停止の最低3シナリオを再現し、検知、切り分け、暫定対応、復旧確認を文書と自動または半自動手順で実行できる。
- lint、type check、unit test、integration test、secret scan、dependency scan、container scan、Helm検証、kind E2Eを同一の検証インターフェースから実行できる。
- README、アーキテクチャ、ADR、SLO、Runbook、Security、Limitations、Demoの各文書が実装と一致する。
- cleanな環境からセットアップ、検証、デモ、後片付けを再現できる。

## Non-goals

- EKS、GKE、AKSその他のクラウド環境へデプロイすること。
- Terraform、Argo CD、Flux、service mesh、multi-clusterを導入すること。
- ユーザー認証、管理画面、マルチテナント、課金、外部公開APIを実装すること。
- 利用者が任意の配送先URLを指定できるようにすること。
- exactly-once配送を保証すること。
- Redis Cluster、Sentinel、managed Redis、multi-AZによる高可用性を構築すること。
- OpenTelemetry traces、Tempo、分散トレーシングを導入すること。
- HPA、NetworkPolicy、SBOM、provenance、GHCR releaseを必須成果物にすること。
- `repo-health-doctor` と統合すること。
- Kubernetes資格対策または機能網羅を目的にすること。
- 外部サービスのsecret、credential、個人情報、業務データを使用すること。
- push、外部公開、クラウド課金を自動で行うこと。

## ハード制約

- 新規開発は単一リポジトリで行い、API、worker、infrastructure、observability、tests、docsを別リポジトリへ分割しない。
- Git submoduleまたは別リポジトリへの実行時依存を作らない。
- アプリケーションはPythonとFastAPIを使用し、queue/status storeにはRedisを使用する。
- ローカルKubernetesはkind、パッケージングはHelm、metricsはPrometheus、dashboardはGrafanaを使用する。
- exact versionは導入環境を確認して固定し、container imageやtoolに`latest` tagを使用しない。
- runtimeは外部クラウド、外部SaaS、実credentialを必要としない。配送先は同一プロジェクト内のmock sinkをdefaultとし、必要なcontrolled endpointへの切替はoperator-controlledなstartup configurationだけで行う。requestから配送先を変更しない。
- 配送保証はat-least-onceと明記し、重複配送の可能性とdownstream側のevent IDによる重複排除方針を説明する。
- livenessは外部依存の一時障害を直接条件にしない。readinessは新規処理を安全に受け付けられるかで判定する。
- payload本文、secret、credential、Redis password、個人情報をログ、fixture、artifact、commitへ含めない。
- containerはnon-root、`allowPrivilegeEscalation: false`、capability drop、read-only root filesystem、RuntimeDefault seccompを原則とする。
- GitHub Actionsを用意する場合、権限を明示して最小化し、未信頼PRでsecretを使用せず、third-party actionを不変参照へ固定する。
- 全主要操作はMakefileまたは同等の単一インターフェースから実行できるようにする。
- 「production-ready」「本番でSLOを達成した」「Kubernetes実務経験」と誤認させる表現を使用しない。
- 7日間でP0を完成させるため、Non-goalsに挙げた機能を途中追加しない。
- push、外部公開、破壊的操作は人間の明示判断なしに行わない。

## 成果物

- API、worker、mock sink、共通domain/queue/delivery/observability modules。
- Redis Streams、retry schedule、dead-letter、event status、idempotencyを扱う実装。
- Dockerfile、Compose構成、`.dockerignore`、placeholderのみの`.env.example`。
- kind設定、Helm chart、values、Kubernetes resources。
- Prometheus scrape設定、ServiceMonitor相当、alert rules、Grafana dashboard。
- unit、integration、kind E2E、incident drillの各テストまたは検証スクリプト。
- lint、type check、test、security scan、chart validation、kind E2Eを実行するCI workflow。
- `README.md`。
- `docs/ARCHITECTURE.md`。
- `docs/SLO.md`。
- `docs/OPERATIONS.md`。
- `docs/SECURITY.md`。
- `docs/LIMITATIONS.md`。
- `docs/DEMO.md`。
- `docs/runbooks/` 配下のRunbook。
- `docs/incidents/` 配下の障害演習記録とblameless postmortemサンプル。
- `docs/adr/` 配下の主要設計判断。
- 前提確認、起動、検証、デモ、診断、後片付けを行うスクリプトまたはMake target。

## Done when

- `make doctor` がexit code 0で終了し、必要なtool、version、Docker接続、ローカルresource前提を確認できる。secretや環境変数の値は表示しない。
- `make lint` と `make typecheck` がexit code 0で終了する。
- `make test` がunit testとintegration testを実行し、exit code 0で終了する。
- `make security` がGitleaks、OSV-Scanner、Trivy相当の検査を実行し、文書化されたfail policyを満たしてexit code 0で終了する。secret findingは0件である。
- `make compose-up` の後に `make smoke` がイベント受付、非同期配送、状態参照を確認してexit code 0で終了する。
- `make e2e-local` がidempotency、retry、dead-letter、worker停止後の未ack message回収を確認してexit code 0で終了する。
- `make cluster-up` と `make deploy` によりkindへHelm installでき、全workloadがreadyになる。
- `make e2e-kind` がkind上で正常配送、retry、復旧、状態参照を確認してexit code 0で終了する。
- `make rollout-smoke` が安全なrolling updateと意図的に失敗させたreleaseからのrollbackを確認してexit code 0で終了する。
- `make observability-smoke` がPrometheus target、主要metrics、Grafana dashboard、最低4件のalert rule、各Runbook参照を確認してexit code 0で終了する。
- `make incident-smoke` がdownstream 5xx、Redis停止、配送中worker停止の3シナリオを実行し、想定signal、復旧、データ消失なしを確認してexit code 0で終了する。
- `make docs-check` が内部リンク、記載コマンド、成果物一覧の整合性を確認してexit code 0で終了する。
- `make verify` がlint、type check、test、security、Helm validation、docs checkをまとめて実行し、exit code 0で終了する。
- `.github/workflows/ci.yml` がローカル検証と同じMake targetを呼び、lint、test、security、Helm validation、kind E2Eを定義している。
- 一時ディレクトリへローカルcloneした状態から、README記載の前提確認、検証、kindデプロイ、デモ、後片付けを実行できる。
- デモ実行後に、未追跡のsecret、log、cache、history、個人情報、業務情報、不要なgenerated artifactが残らない。
- READMEとLimitationsに、at-least-once、重複可能性、単一Redis、単一kind cluster、ローカル測定値、未実装範囲が明記されている。
- docsのSLI/SLO、alert、Runbook、incident drillが相互参照され、実装上のmetric名および手順と一致する。
- 3分以内でアーキテクチャ、主要failure mode、health semantics、検証結果、制約を説明できるデモ手順が存在する。
- 人間による最終レビューで、実装していないことを実装済みと表現していないこと、個人検証を実務または本番実績と誤認させないことを確認する。

## 未確認

- Docker、Docker Compose、kind、kubectl、Helm、Make、Git、Bashの導入状態とversion。
- ローカルマシンでkind、Redis、Prometheus、Grafanaを同時起動できるCPU、memory、disk余力。
- Pythonの利用可能versionと採用するdependency lock手段。
- GitHub上で`hooklane`というrepository名を利用できるか。
- 公開時のlicense。
- GitHubへのpush、公開repo化、Actions実行をこの1週間の範囲に含めるか。
- container scanで検出される脆弱性と、修正可能性または例外判断。
- `docs/PLAN.md` と `docs/STATUS.md` の詳細フォーマットはKICKOFF生成後に確認する。

## 仮定

- 新規プロジェクトとして開始し、既存ソースコードとの互換性維持は不要である。
- project名とrepo名は`hooklane`とする。
- 既存の`repo-health-doctor`は独立repoのまま維持し、この1週間は連携させない。
- runtimeに必要なserviceはすべてローカルcontainerまたはkind内で完結できる。
- GitHubへpushしなくても、同等の検証をローカルで完了できる構成にする。
- 1週間ではP0の一貫したvertical sliceを優先し、P1候補の追加より再現性、障害対応、文書整合性を優先する。
- Goal Loopでは1周につき検証可能なfeature 1件を処理し、書き込みagentは同一worktreeで同時に1体だけとする。

## 人間判断が必要なこと

- repoをpublicにするか、公開時期をいつにするか。
- licenseを何にするか。
- GitHubへのpushとActions実行を許可するか。
- vulnerability scanで即時修正できないfindingが出た場合、修正、base image変更、期限付き例外、公開延期のどれを選ぶか。
- README、デモ、スクリーンショット、リリース文面が転職目的を前面に出さず、技術プロジェクトとして自然に見えるか。
- P0完了後にHPA、NetworkPolicy、SBOM、provenance、GHCR releaseのいずれかを追加するか。
