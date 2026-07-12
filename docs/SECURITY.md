# Hooklane security model

## Scope and assurance boundary

この文書はrepositoryに実装されたlocal Compose、single-node kind、Helm chart、CI contractのsecurity controlと残存riskを説明する。Internet-facing production deployment、identity、tenant isolation、cloud control plane、external downstream、organization incident responseを評価したものではない。設計上の制約は[Limitations](LIMITATIONS.md)を併読する。

Threat modelは次を対象にする。

- Untrusted HTTP inputがapplication、log、metric、downstream destinationへ与える影響
- Secret-like value、payload、identifierのrepository、log、diagnosticsへの漏えい
- Dependency、container image、Kubernetes manifest、CI workflowの既知risk
- Local containerまたはPod compromise時の権限縮小
- Redis、worker、downstream failure時のfalse successとaccepted event loss

Authentication、authorization、encryption termination、DDoS protection、supply-chain provenance signing、runtime intrusion detectionは実装していない。

## Secret and sensitive-data policy

Repositoryは実値credentialを保持せず、`.env.example`はempty placeholderだけを許可する。Contractは`make env-example-check`で検証する。Secret値、`.env`値、cookie、private key、token、personal informationをlog、metric、diagnostics、test artifactへ出さない。

KubernetesではRedis connection設定をSecret resourceからPodへ注入するが、Secret objectの値をdocumentationやdiagnosticsへ展開しない。Local demonstrationはcredentialを要求せず、external secret managerとの統合は範囲外である。

## Request and log handling

APIはrequest schemaと`Idempotency-Key` lengthをsystem boundaryで検証する。Downstream URLはrequestから受け取らず、固定allowlistのdestinationだけを使用する。

API、worker、mock sinkは共通のstructured JSON log contractを使う。許可fieldはtimestamp、level、service、event、request ID、event ID、attempt、status、outcome、reason code、durationなどに限定する。

- Payload本文を記録しない。
- `Idempotency-Key`生値を記録しない。Persistenceにはdigestを使う。
- Credential、Redis password、Redis URL、cookieを記録しない。
- Arbitrary exception messageやinternal stack traceを公開logまたは通常responseへ流さない。
- Errorは有限集合のreason codeへ分類する。
- Diagnostics sanitizerはrequest bodyとconnection contentを除外する。

Contract実装は[`logging.py`](../src/hooklane/observability/logging.py)、検証は[`test_logging.py`](../tests/unit/test_logging.py)を正本とする。

## Metrics and cardinality

Application metricsは`hooklane_` prefixを使い、labelをmethod、route template、status class、outcome、reason code、service、operationの有限集合に限定する。event ID、request ID、`Idempotency-Key`、raw URL、payload typeのunbounded value、exception message、user inputをlabelへ入れない。

Metrics endpointはhealth endpointから分離し、payloadやsecretをexportしない。Contract実装は[`metrics.py`](../src/hooklane/observability/metrics.py)、cardinality testは[`test_metrics.py`](../tests/unit/test_metrics.py)にある。

## Container hardening

API、worker、mock sinkはpinned base imageからmulti-stage buildし、numeric non-root userで実行する。ComposeとHelmの既定policyは次の通りである。

- privilege escalationを許可しない。
- Linux capabilitiesをすべてdropする。
- root filesystemをread-onlyにし、必要な一時pathだけをwritableにする。
- Kubernetesでは`RuntimeDefault` seccomp profileを使う。
- Liveness、readiness、resource request/limit、termination graceを明示する。
- Host portを公開するCompose serviceはloopbackへbindする。

Redis upstream entrypoint、Prometheus TSDB、Grafana state、single-replica PDBなどの限定例外は[`container-policy.json`](../container-policy.json)にscopeとreasonを記録する。これらはscanner findingの免除ではない。

## Kubernetes identity and RBAC

Application ServiceAccountはautomatic token mountを無効にし、API、worker、mock sink、Redis、GrafanaはKubernetes API tokenを必要としない。Prometheusだけがannotated Pod discoveryのためにshort-lived projected tokenを明示mountし、namespace内Podの`get`、`list`、`watch`だけを許可するRoleとRoleBindingを使う。

RBAC実装は[`observability-rbac.yaml`](../charts/hooklane/templates/observability-rbac.yaml)、ServiceAccount設定は[`serviceaccount.yaml`](../charts/hooklane/templates/serviceaccount.yaml)にある。ClusterRoleやautomatic broad token mountは使用しない。

## Network exposure

ComposeのAPIとRedis host portはloopback限定である。kind API mappingもloopback限定で、Grafana、Prometheus、mock sink、Redis Serviceはcluster-localである。Manual port-forwardはloopbackで一時利用し、確認後に停止する。

Kubernetes NetworkPolicy、ingress TLS、service mesh、egress firewallは実装していない。したがってclusterをuntrusted networkまたはshared production environmentとして扱わない。

## Dependency and tool pinning

Python direct dependencyは[`pyproject.toml`](../pyproject.toml)、fully resolved dependencyは[`requirements.lock`](../requirements.lock)を正本とする。Runtime、scanner、kind node versionは[`toolchain.toml`](../toolchain.toml)、container image pinと例外は[`container-policy.json`](../container-policy.json)に置く。Application、Redis、Prometheus、Grafana、kind node imageは固定tagまたはdigestを使う。

Generated lock fileやvendored binaryを手作業で変更しない。Dependency更新時はlock再生成、quality gate、security gate、image scanを同じchangeで再実行する。

このrepositoryはsource code、Dockerfile、Helm chart、configuration、documentation、検証手順だけを配布し、prebuilt container image、registry artifact、release archive、binaryを提供しない。Application imageは利用者がlocal buildする。Runtime dependency、base image、Redis、Prometheus、Grafana、validation toolには各上流licenseとnoticeが適用される。確認範囲とversion正本は[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)に記録する。

## Security gates

[`security-policy.json`](../security-policy.json)はfail-closed policyのmachine-readable正本である。

| Gate | Scope | Failure policy |
|---|---|---|
| Gitleaks | Git historyとworking treeのsecret scan、full redaction | findingまたはtool failureでnon-zero |
| OSV-Scanner | `requirements.lock`のknown vulnerability | finding、parse failure、timeout、tool failureでnon-zero |
| Trivy filesystem | lock済みPython dependency | HIGHまたはCRITICAL、DB failure、tool failureでnon-zero |
| Trivy image | API、worker、mock sinkの全local image | OS packageとlanguage dependencyのHIGHまたはCRITICALでnon-zero |
| Kubeconform | rendered Kubernetes resourceとdeprecated API | invalidまたはunknown policy violationでnon-zero |
| Helm/schema contract | default、observability on/off、negative values、securityContext、probe、resource、PDB、rollout | contract違反でnon-zero |

実行interfaceは次である。

```bash
make security-secret
make security-dependency
make security-filesystem
make security-image
make security
make chart-validate
make verify
```

Scanner unavailable、timeout、database download failure、parse failureをfinding 0として扱わない。`security-policy.json`のsecurity finding exception listは空であり、自動ignoreを追加しない。例外が必要な場合は対象、理由、期限、action categoryを人間が判断し、policy changeとしてreviewする。

## GitHub Actions controls

[`ci.yml`](../.github/workflows/ci.yml)はpull requestとmain branchを対象に、local Make targetを呼び出す。Workflowのcontrolは次の通りである。

- Top-level permissionはrepository contents readだけで、jobごとに拡張しない。
- `pull_request_target`を使用しない。
- Untrusted pull requestでsecretを参照しない。
- Checkout、Python setup、artifact upload actionをfull commit SHAで固定する。
- Workflow内にquality、security、kind手順を複製せず、`make verify`と`make e2e-kind`を呼ぶ。
- Concurrency、timeout、failure diagnostics、always cleanupを明示する。
- Container imageをexternal registryへpushしない。

Static contractは`make ci-contract`で検証する。GitHub hosted Actions上の実行はpushしていないため未確認である。

## Failure safety

Redisへeventとstatusを永続化できない場合、APIは`202 Accepted`を返さない。Readinessはfalseとなるが、livenessはprocess healthを表し続ける。Worker side effect後の停止ではpending messageを別workerがclaimし、同じevent IDを再配送する可能性がある。これはat-least-once contractであり、downstream event-ID deduplicationが必要である。

Failure behaviorの検証receiptは[downstream 5xx](incidents/downstream-5xx.md)、[Redis outage](incidents/redis-outage.md)、[worker stop](incidents/worker-stop.md)にある。

## Residual risks

- Authenticationとtenant authorizationがないため、local boundaryを越えた公開に適さない。
- Single Redis、single-node kind、default single workerにはHAがない。
- At-least-once deliveryはduplicate side effectを起こし得る。
- Redis dataにTTL、backup、restore、encryption-at-rest policyがない。
- NetworkPolicy、TLS、external secret manager、image signing、SBOM attestationを実装していない。
- Grafana anonymous Viewerはcluster-local local-demo例外であり、外部公開に使えない。
- Scanner resultはdatabase更新で変化するため、過去のfinding 0は将来のfinding 0を保証しない。
- Hosted CI、cloud runtime、long-running adversarial loadは未検証である。
- Source-only公開はproduction readiness、supply-chain certification、runtime security保証を意味しない。

## Security reporting

Public reporting endpointとrepository owner情報はこのlocal repositoryから確定できない。問題を発見した場合はsecret値をissue、log、artifactへ貼らず、file path、classification、再現に必要な非機密情報、影響範囲だけをrepository maintainerへprivate channelで伝える。Private channelが未設定の場合は外部公開前に人間がreporting methodを決める。

Tracked secretを検知した場合は値を再掲せず、path、key name、line、recommended containmentだけを記録し、history rewriteやremote操作は人間判断とする。
