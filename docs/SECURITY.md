# Hooklane security

## 対象範囲

この文書はrepositoryに実装したDocker Compose、single-node kind、Helm chart、CI contractのsecurity controlと残存riskを扱う。設計上の制約は[制約](LIMITATIONS.md)を正本とする。

対象とするthreat modelは次の通り。

- 信頼しないHTTP inputがapplication、log、metric、downstream destinationへ与える影響
- secret-like value、payload、identifierのrepository、log、diagnosticsへの漏えい
- dependency、container image、Kubernetes manifest、CI workflowの既知risk
- local containerまたはPod compromise時の権限縮小
- Redis、worker、downstream failure時のfalse successとaccepted event loss

authentication、authorization、encryption termination、DDoS protection、supply-chain provenance signing、runtime intrusion detectionは実装していない。

## secretとsensitive dataの方針

repositoryは実値credentialを保持せず、`.env.example`はempty placeholderだけを許可する。contractは`make env-example-check`で検証する。secret値、`.env`値、cookie、private key、token、personal informationをlog、metric、diagnostics、test artifactへ出さない。

KubernetesではRedis connection設定をSecret resourceの`secretKeyRef`からPodへ注入できる。Terraform foundationでは同じ境界をAWS Secrets ManagerとECS task secret injectionで定義する。remote state bootstrap S3 bucket、artifact stageのECR、ECS taskを0 taskに保つfoundation stage、runtime taskへのSecret injectionは明示承認の下で作成・security contractを検証した。rotation、Secret lifecycle、長期接続安定性は未実証である。Secret objectの値をdocumentationやdiagnosticsへ展開せず、既定のlocal fallbackはcredentialを要求せず、ConfigMapへRedis URLを配置しない。

## requestとlogの扱い

APIはrequest schemaと`Idempotency-Key` lengthをsystem boundaryで検証する。downstream URLはrequestから受け取らず、起動時environmentのoperator-controlled endpointだけを使用する。未指定時はproject-owned mock sinkを使い、URL内credentialは拒否する。requestごとの配送先選択は行わない。

API、worker、mock sinkは共通のstructured JSON log contractを使う。許可fieldはtimestamp、level、service、event、request ID、event ID、attempt、status、outcome、reason code、durationなどに限定する。

- payload本文を記録しない
- `Idempotency-Key`生値を記録しない。persistenceにはdigestを使う
- credential、Redis password、Redis URL、cookieを記録しない
- arbitrary exception messageやinternal stack traceを公開logまたは通常responseへ流さない
- errorは有限集合のreason codeへ分類する
- diagnostics sanitizerはrequest bodyとconnection contentを除外する

contract実装は[`logging.py`](../src/hooklane/observability/logging.py)、検証は[`test_logging.py`](../tests/unit/test_logging.py)を正本とする。

## metricsとcardinality

application metricsは`hooklane_` prefixを使い、labelをmethod、route template、status class、outcome、reason code、service、operationの有限集合に限定する。event ID、request ID、`Idempotency-Key`、raw URL、payload typeのunbounded value、exception message、user inputをlabelへ入れない。

metrics endpointはhealth endpointから分離し、payloadやsecretをexportしない。contract実装は[`metrics.py`](../src/hooklane/observability/metrics.py)、cardinality testは[`test_metrics.py`](../tests/unit/test_metrics.py)にある。

## container hardening

API、worker、mock sinkはpinned base imageからmulti-stage buildし、numeric non-root userで実行する。ComposeとHelmの既定policyは次の通り。

- privilege escalationを許可しない
- Linux capabilitiesをすべてdropする
- root filesystemをread-onlyにし、必要な一時pathだけをwritableにする
- Kubernetesでは`RuntimeDefault` seccomp profileを使う
- liveness、readiness、resource request／limit、termination graceを明示する
- host portを公開するCompose serviceはloopbackへbindする

Redis upstream entrypoint、Prometheus TSDB、Grafana state、single-replica PDBなどの限定例外は[`container-policy.json`](../container-policy.json)にscopeとreasonを記録する。scanner findingの免除ではない。

## Kubernetes identityとRBAC

application ServiceAccountはautomatic token mountを無効にし、API、worker、mock sink、Redis、GrafanaはKubernetes API tokenを必要としない。Prometheusだけがannotated Pod discoveryのためにshort-lived projected tokenを明示mountし、namespace内Podの`get`、`list`、`watch`だけを許可するRoleとRoleBindingを使う。

RBAC実装は[`observability-rbac.yaml`](../charts/hooklane/templates/observability-rbac.yaml)、ServiceAccount設定は[`serviceaccount.yaml`](../charts/hooklane/templates/serviceaccount.yaml)にある。ClusterRoleやautomatic broad token mountは使用しない。

## network exposure

ComposeのAPIとRedis host portはloopback限定。kind API mappingもloopback限定で、Grafana、Prometheus、mock sink、Redis Serviceはcluster-local。manual port-forwardはloopbackで一時利用し、確認後に停止する。

Kubernetes NetworkPolicy、ingress TLS、service mesh、egress firewallは実装していない。untrusted networkまたはshared production environmentのsecurity boundaryとしては扱わない。

## dependencyとtoolの固定

Python direct dependencyは[`pyproject.toml`](../pyproject.toml)、fully resolved dependencyは[`requirements.lock`](../requirements.lock)を正本とする。runtime、scanner、kind node versionは[`toolchain.toml`](../toolchain.toml)、container image pinと例外は[`container-policy.json`](../container-policy.json)に置く。application、Redis、Prometheus、Grafana、kind node imageは固定tagまたはdigestを使う。

dependency更新時はlock再生成、quality gate、security gate、image scanを同じchangeで再実行する。配布範囲とthird-party noticeは[README](../README.md#配布範囲)と[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)を参照する。

## security gate

[`security-policy.json`](../security-policy.json)はfail-closed policyのmachine-readable正本。

| gate | 対象 | 失敗時の扱い |
|---|---|---|
| Gitleaks | Git historyとworking treeのsecret scan、full redaction | findingまたはtool failureでnon-zero |
| OSV-Scanner | `requirements.lock`のknown vulnerability | finding、parse failure、timeout、tool failureでnon-zero |
| Trivy filesystem | lock済みPython dependency | HIGHまたはCRITICAL、DB failure、tool failureでnon-zero |
| Trivy image | API、worker、mock sinkのlocal image | OS packageとlanguage dependencyのHIGHまたはCRITICALでnon-zero |
| Kubeconform | rendered Kubernetes resourceとdeprecated API | invalidまたはunknown policy violationでnon-zero |
| Helm／schema contract | default、observability on/off、negative values、securityContext、probe、resource、PDB、rollout | contract違反でnon-zero |

```bash
make security-secret
make security-dependency
make security-filesystem
make security-image
make security
make chart-validate
make verify
```

scanner unavailable、timeout、database download failure、parse failureをfinding 0として扱わない。`security-policy.json`のsecurity finding exception listは空であり、自動ignoreを追加しない。

## GitHub Actions control

[`ci.yml`](../.github/workflows/ci.yml)はpull requestとmainを対象に、local Make targetを呼び出す。

- top-level permissionはrepository contents readだけで、jobごとに拡張しない
- `pull_request_target`を使わない
- untrusted pull requestでsecretを参照しない
- checkout、Python setup、artifact upload actionをfull commit SHAで固定する
- quality、security、kind手順をworkflowへ複製せず、`make verify`と`make e2e-kind`を呼ぶ
- concurrency、timeout、failure diagnostics、always cleanupを明示する
- container imageをexternal registryへpushしない

static contractは`make ci-contract`で検証する。GitHub hosted Actionsではquality / security / chart gatesとkind delivery and recovery E2Eを実行済み。

## failure safety

Redisへeventとstatusを永続化できない場合、APIは`202 Accepted`を返さない。readinessはfalseとなるが、livenessはprocess healthを表し続ける。worker side effect後の停止ではpending messageを別workerがclaimし、同じevent IDを再配送する可能性がある。これはat-least-once contractであり、downstreamのevent ID重複排除が必要となる。

failure behaviorの検証根拠は[downstream 5xx](incidents/downstream-5xx.md)、[Redis outage](incidents/redis-outage.md)、[worker stop](incidents/worker-stop.md)にある。

## 残存risk

- authenticationとtenant authorizationがないため、local boundaryを越えた公開に適さない
- single Redis、single-node kind、default single workerにはHAがない
- at-least-once deliveryはduplicate side effectを起こし得る
- Redis dataにTTL、backup、restore、encryption-at-rest policyがない
- NetworkPolicy、TLS、external secret manager、image signing、SBOM attestationを実装していない
- Grafana anonymous Viewerはcluster-localの検証用例外であり、外部公開に使えない
- scanner resultはdatabase更新で変化するため、過去のfinding 0は将来のfinding 0を保証しない
- cloud runtime、long-running adversarial loadは未検証

## 脆弱性の報告

GitHub private vulnerability reportingは現在無効で、repositoryにはprivate reporting channelを設定していない。公開Issueへsecret、credential、脆弱性の詳細、再現payloadを記載しない。private reporting methodの追加はrepository maintainerの判断とする。

tracked secretを検知した場合は値を再掲せず、path、key name、line、recommended containmentだけを記録する。history rewriteやremote操作は人間判断とする。
