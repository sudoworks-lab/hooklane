# ADR 0006: Python application languageの維持

## 状態

承認済み（Approved）

## 背景（Context）

Hooklaneはlocal CLIではなく、Webhook API、非同期delivery worker、Redis Streamsを中心としたreliability serviceである。現在の構成は、Python 3.12、FastAPI、Pydantic、Redis Streams、async HTTP delivery、idempotency、retry／dead-letter、pending recovery、graceful shutdown、structured logs、Prometheus metrics、availability monitoring、incident drill、operational signal normalizationで構成される。

現行mainでは、API／worker availability monitoring、worker availability incident evidence normalization、downstream delivery failure evidence normalizationも既存のPython runtime、test、incident toolingの上に追加されている。`pyproject.toml`はPython 3.12とFastAPI、Pydantic、Redisなどのdependencyを固定し、`src/hooklane/`、`tests/`、`scripts/`が同じapplicationと検証境界を構成している。

Portfolioのlanguage diversity自体はHooklaneのproduct requirementではない。Goなどへのrewriteを、language portfolioの見た目だけを理由に行うと、API contract、Redis state、idempotency、retry／DLQ、pending recovery、shutdown、observability、incident evidenceの回帰リスクと移行コストを負うことになる。現行runtime、test、incident evidenceから、Python runtime自体がcapacity、latency、memory、reliabilityのmaterial bottleneckであるというverified evidenceはない。

## 決定（Decision）

Current supported application languageとしてPythonを維持する。既存architecture、framework integration、tests、incident drills、operational evidenceを継続利用し、Goなどへのrewriteは行わない。

これはPythonが常に最良であるという判断ではない。現在のsystem responsibilityとverified evidenceに対して、rewriteを正当化するmaterial requirementが存在しないという判断である。

## なぜPythonを維持するか（Why Python remains appropriate）

- FastAPIとPydanticは、Webhookのrequest／response validation、型付きschema、HTTP API contractを現在の境界に適合させている。
- 現在の主要workloadはRedisとHTTPを使うasync I/O中心であり、API、worker、mock sinkが同じPython runtimeで実装されている。
- application、test、incident drill、observability validationをPythonで一貫して扱え、runtime contractと検証contractの差を増やさない。
- 既存のunit／integration test、availability monitoring、incident drill、signal normalization、documentation evidenceに十分な検証投資がある。
- rewriteは、受付、永続化、idempotency、retry／DLQ、pending recovery、graceful shutdown、metrics、incident evidenceのcontract回帰を検証し直す移行コストを伴う。
- 現時点で、Python performanceがcapacityまたはSLOのverified constraintになっている測定結果はない。PythonがGoより高速である、または一般に優れているとは主張しない。

## Go alternative

Goは有効な代替案であり、次の利点を持ち得る。

- goroutineとchannelを中心とするconcurrency modelを利用できる。
- runtime dependencyを含まないsingle executableとして配布しやすい。
- startup time、runtime footprint、deployment densityを改善できる可能性がある。
- native processとしてのlifecycleと配布手順を単純化できる可能性がある。

一方、現行のverified evidenceは、それらをrewriteコストより優先すべきmaterial requirementとして示していない。Goへの移行では、FastAPI／PydanticのAPI semantics、Redis async behavior、既存test、incident drill、operational evidenceを再実装し、contract regressionを再検証する必要がある。したがって、Goの一般的な利点だけでは移行理由にならない。

## Evidence boundary

このdecisionの根拠は、現行の`pyproject.toml`、`src/hooklane/`、`tests/`、`scripts/`、[`docs/PLAN.md`](../PLAN.md)、[`docs/RELEASE_EVIDENCE.md`](../RELEASE_EVIDENCE.md)、およびcurrent mainのruntime／observability／incident変更である。これらはlocal、kind、tracked verification contractの証拠であり、cloud productionやproduction-scale performanceの証明ではない。

## 再評価トリガー（Migration triggers）

次のいずれかを、対象workload、固定したrequired target、再現可能な測定結果で確認した場合に再評価する。

- profilingでPython runtime overheadがend-to-end latencyまたはCPU使用量を支配し、必要なp95／p99 latency targetを満たせない。
- 固定したthroughputまたはconcurrency targetを、Python構成の調整後も再現可能な測定で満たせない。
- memory footprintまたはdeployment densityがhard constraintとなり、測定値が許容budgetを超える。
- startup time、binary distribution、runtime dependencyの削減がmaterialなoperational requirementとなり、Python構成では満たせない。
- current SLOまたはcapacity requirementを、再現可能なruntime／load evidenceでPython構成が満たせない。
- migration、contract regression、運用切替のcostと、測定された性能または運用上のbenefitを比較し、migration benefitが明確に上回る。

「Go-firstであること」やportfolioの見た目だけではmigration triggerにならない。

## 結果（Consequences）

### Positive

- existing API、delivery、reliability、observability、incident contractsとevidenceを保持できる。
- material requirementのないrewriteを避け、current development／operations modelを維持できる。
- Python runtimeの上で蓄積したtest、drill、signal normalizationの検証資産を継続利用できる。

### Trade-offs

- Python runtimeとcontainer dependencyを保持する。
- single native executableによる配布や、Goで得られる可能性のあるstartup／footprint上の利点は採用しない。
- 将来のscale、capacity、deployment requirementが変わった場合は、測定結果に基づく再評価が必要になる。

## Non-claims

- production-scale performanceを証明するdecisionではない。
- PythonがGoより一般に優れている、または高速であるとは主張しない。
- permanentまたはirreversibleなdecisionではない。
- portfolio appearanceを理由にしたdecisionではない。
