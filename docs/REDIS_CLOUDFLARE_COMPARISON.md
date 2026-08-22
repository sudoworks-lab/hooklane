# Redis StreamsとCloudflare delivery backendの比較

## 結論

両backendは同じHooklane外部contractを異なるprimitiveで実現する。Redis backendはdefault implementationであり、Compose、kind、Helm、Prometheus、Grafana、Runbook、incident drillまでをrepositoryで持つ。Cloudflare backendはPython Worker、D1、Queuesのlocal-only spikeであり、idempotency、retry、dead-letter、at-least-once、D1からQueueへのoutbox recoveryをcodeとlocal emulationで示すが、production migrationや運用同値性は示さない。

Cloudflareを優位と結論づける根拠はない。managed primitiveによってself-hosted queue運用を減らせる可能性がある一方、D1とQueueのcross-service gap、provider DLQとの二層状態、account設定、telemetry変換、Python Workers betaという別の運用負荷が生じる。

## 維持するdelivery contract

両backendで次を変えない。

- `POST /v1/events`はdurable acceptance後だけ202を返し、未知eventは404、idempotency conflictは409、dependency failureは503とする
- statusは`queued`、`delivering`、`retry_scheduled`、`delivered`、`dead_letter`、attempt countはdelivery開始ごとに増える
- same `Idempotency-Key`とsame canonical requestはsame event ID、different requestは409、並行判定はatomicとする
- HTTP 2xxだけをsuccess、429、5xx、timeout、connection failureをretryable、3xxとその他4xxをnon-retryableとする
- bounded retry後はterminal dead-letterとし、downstream success前にackしない
- at-least-onceであり、downstream side effect後の同一event ID再配送を許容する
- requestからdelivery destinationを指定させず、payload、raw Idempotency-Key、credential、unsafe exception textをlogやmetricへ出さない
- livenessとdependency readinessを分離する

## Primitiveとsemanticの比較

| 比較軸 | Redis backend | Cloudflare local spike | 証拠とtrade-off |
|---|---|---|---|
| ingress | Python 3.12 FastAPI API | Python 3.13 Worker上のFastAPI ASGI adapter | public request schemaとstatus vocabularyのparity testをroot suiteで固定する。Python Workersはbetaでruntimeを分離する |
| durable state | Redis hash、Stream、sorted set、dead-letter stream | D1 event metadata、status、attempt、payload chunks、idempotency index、outbox | Redisはsingle instance／PVC、Cloudflareはsingle D1 databaseの制約を持ち、どちらもmulti-region durabilityをこのrepositoryでは実証していない |
| queue | Redis Streams consumer group | Cloudflare Queuesへ`event_id`だけを送信 | どちらもat-least-once。Queue messageにpayloadを複製しないため128 KB message limitはpayload contractへ波及しない |
| idempotency | Redis Luaでstatus、stream、digest mappingをatomic create/reuse/conflict | D1 unique constraintとbatch transactionでhashed key／fingerprintをatomic create/reuse/conflict | 20 concurrent requestで両者のone logical event contractを検証する。raw keyは保存しない |
| retry | Redis sorted setへdue timeを永続化し、workerがack/requeue | Queue `retry()` delayとD1 `retry_scheduled` status、delivery token／lease CAS | 同じ分類とmaximum attemptsを維持し、stale attemptはretry stateを上書きできない。provider retry回数とapplication attempt countは同じ意味ではない |
| DLQ | Luaがsource entry、error class、attempt、source stream IDをdead-letter streamへ追加し、status更新とsource ackを同じRedis operationで行う | downstream failureはD1 `dead_letter`をcanonical terminal statusにしてQueue messageをack。handler自体が完了不能ならprovider DLQへ移る | Cloudflareはapplication terminal stateとprovider DLQの二層。provider DLQ inspection／replay／D1 reconciliationは未実装 |
| acceptance atomicity | Redis Luaがevent status、idempotency mapping、Stream enqueueを単一operationで確定 | D1 batchがevent、payload chunks、idempotency、pending outboxをtransactionalに確定後202。Queue sendはcommit後 | Cloudflareのcross-service非原子性はpending outboxでloss-safeにするが、duplicate sendは残る |
| duplicate boundary | side effect後かつRedis status／ack前の停止でconsumer-group pendingを再claim | side effect後かつD1 delivered transition／Queue ack前のfailureでQueueが再配送。terminal duplicateはD1 CASでsinkなしack | failure windowではsame event IDを維持し、terminal後のduplicateではstate／attemptを不変にする。downstream側deduplicationはなお必要 |
| payload handling | payloadをRedis Stream entryに保持。application-level byte limitはない | canonical payload JSONを1.5 MB以下のD1 chunk rowへ分割し、event metadata／全chunk／outboxを同じbatchでcommit | 2,065,536-byte payloadをlocal D1からdelivery済み。D1 single-row 2 MB gapを解消し、外部API上限やR2を追加しない |
| failure recovery | retry scheduler、consumer-group pending claim、dead-letter stream、incident drill | pending outboxのD1 compare-and-set lease、Queue retry、provider DLQ | 20 concurrent repairは1 owner／1 Queue send。lease expiry後はloss回避のためduplicateを許容する |
| observability | application Prometheus metrics、Grafana、alerts、SLI／SLO、Runbook、structured logs | content-free transition logs。Workers、Queues、D1のprovider metrics／tracesは候補のみ | provider telemetryを既存Prometheus semanticsへ変換する実装、alert、dashboard、retention、notificationは未実装 |
| operational complexity | API、worker、Redis、mock sink、Prometheus、Grafana、HelmとLua state machineをrepositoryで運用 | Worker、D1、Queue、Cron、DLQ、binding、migration、outbox reconciliationをaccount側で運用 | self-hosted component数だけでは比較できない。Cloudflare resource security、cost、quota、migration、provider outage手順が未検証 |
| local reproducibility | Compose、kind、Helm、incident／observability target | locked Python 3.13、Wrangler、workerdのtemporary D1／Queueと既存mock sink | Cloudflare flowはcredential-freeだがremote behavior、account policy、billingを再現しない |
| known limits | single Redis、single node、HA／backup／retentionなし | D1 per-row／database／query limits、Workers request／memory limits、Queue orderingなし、Python Workers beta | production capacity、retention、multi-region、real downstream、SLO実績は双方とも未確認 |

## Payload contractの決定

### 採用: transactional D1 payload chunks

現行`EventRequest`にapplication-level payload byte limitはない。このportfolio spikeでは新しい413 semanticsを加えず、canonical payload JSONをUTF-8境界で1,500,000 bytes以下に分割し、`event_payload_chunks`へ保存する。event metadata、全payload chunk、pending outbox、idempotency lookupは同じD1 `batch()` transactionに入り、いずれかのstatementが失敗すればacceptance全体をrollbackして202を返さない。consumerはevent IDでchunkを順に読み、payloadを再構成する。

[D1 limits](https://developers.cloudflare.com/d1/platform/limits/)はstring、BLOB、rowを2,000,000 bytesに制限する。1.5 MB chunkはrow metadata分のheadroomを残す。[D1 batch](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch)はstatementをtransactionとして順次実行し、失敗時にsequence全体をrollbackする。local flowでは2,065,536-byte payloadが202後に`delivered`となり、Queue messageはevent ID referenceのままである。

この決定はpayloadを無制限と保証しない。CloudflareにはWorkers request body、memory、D1 query-per-invocation、database sizeのplatform limitがあり、production supported maximumは未決定である。portfolioで解消したのはD1 single-row 2 MBによる即時contract gapである。

### 不採用: explicit API payload limit

明示的上限は運用しやすいが、現行public modelにない新しい拒否contractを加える。traffic、cost、downstream payload実績のないlocal spikeで値を固定すると既存contractを根拠なく狭めるため、このrunでは採用しない。production gateではplan、memory、cost、abuse protectionを根拠に上限と413 responseを決める必要がある。

### 不採用: R2 payload indirection

[R2](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)は大きいobjectを扱えるが、R2 writeとD1 acceptanceを単一transactionにできない。R2-firstではD1 failure時のorphan cleanup、D1-firstではpayload未永続のaccepted eventを避ける追加state machineが必要になる。D1 chunk transactionで2 MB single-row gapを解消できたため、その新しいcoordination、retention、authorization、costをportfolio scopeへ追加しない。

## Outbox ownershipとfailure boundary

acceptance後のimmediate sendとscheduled repairは次のstateを共有する。

1. D1 acceptance transactionがpending outboxを作る。
2. immediate Queue sendが失敗すればpendingのまま202を返す。D1 acceptanceは成立済みでevent lossはない。
3. repairはD1のatomic `UPDATE ... RETURNING`でpending rowへrandom claim tokenと30秒leaseを設定する。同時repairは同じ有効leaseをclaimできない。
4. Queue send failureはclaimをreleaseする。send successはclaim tokenを条件にsentへcompare-and-setする。
5. send success後にD1 transitionが失敗、またはownerが消失した場合はlease expiry後に再claimする。同じevent IDのduplicateはあり得るが、event lossを避ける。

delivery attemptにもD1 token／leaseを持たせる。active duplicateはretryへ戻し、期限切れownerだけがclaimを更新する。terminal stateは再配送時にsinkを呼ばずackし、stale attemptのsuccess／failureはtoken CAS失敗後にcurrent stateを返す。これによりat-least-onceのfailure windowは維持しながら、attempt 6以降とterminal stateの逆行を防ぐ。

unit failure injectionとWrangler local flowの双方で20 concurrent repairを実行し、1 logical outbox dispatch、send count 1、最終`delivered`を確認する。exactly-once、global scheduler election、production lease tuningは保証しない。

## DLQ semantics

Redis dead-letter streamとCloudflare provider DLQは同じものではない。

- Redisはapplicationが理解したdownstream failureについて、terminal status、attempt、error class、source stream ID、source ackをLuaでまとめる。operator replay UIはRedis側にもないが、dead-letter streamがcanonical evidenceになる。
- Cloudflareはapplicationが理解した4xxまたはmaximum attemptsをD1 `dead_letter`へ確定してQueue messageをackする。これがpublic statusのcanonical stateである。
- poison reference、D1 unavailable、uncaught consumer failureのようにapplication transitionを完了できないmessageは`max_retries=4`後にprovider DLQへ移る。[Cloudflare DLQ](https://developers.cloudflare.com/queues/configuration/dead-letter-queues/)は通常のQueueであり、consumerなしでは4日保持される。
- provider DLQのmessageが必ずD1 `dead_letter`になっているとは限らない。inspection、replay、retention policy、D1 reconciliation、operator approval workflowはproduction gapとして残す。

## Observability semantic mapping

| 観測目的 | Redis／Prometheus current contract | Cloudflare-native候補 | portfolioで確認したこと | production gap |
|---|---|---|---|---|
| request | `hooklane_http_requests_total`、`hooklane_http_request_duration_seconds`、`hooklane_enqueue_total` | Workers request／invocation metrics、Workers Logs | HTTP statusとcontent-free acceptance transition | valid acceptanceの分母、202／409／503、route単位latencyを既存SLIへ変換するcustom aggregation |
| queue backlog | `hooklane_queue_depth`、`hooklane_oldest_queued_event_age_seconds`、`hooklane_pending_messages` | Queues backlog messages／bytes、oldest timestamp、lag、consumer concurrency | Queue reference deliveryとoutbox pending state | D1 pending outboxとQueue backlogを一つのloss-risk signalとして結合するquery／alert |
| delivery outcome | `hooklane_delivery_attempts_total`、`hooklane_delivery_outcomes_total`、`hooklane_delivery_completion_total` | structured transitions、D1 terminal status、Workers queue invocation logs | delivered、retry_scheduled、dead_letter、attempt parity | event単位success rateと60秒completion SLIの集計、retention、dashboard |
| retry | `hooklane_retry_scheduled_total`とreason code | Queues retry count、`retry_scheduled` transition | 5xx、maximum attempts、same event ID | provider handler retryとapplication downstream retryを分離したmetric／alert |
| dead-letter | `hooklane_dead_letter_total`、`HooklaneDeadLetterIncreasing`、status、dead-letter stream | D1 `dead_letter`、Queue operation outcome `dlq`、provider DLQ backlog | non-retryableとmaximum attemptsのterminal status | D1とprovider DLQのreconciliation、inspection／replay Runbook、alert |
| failure diagnosis | `hooklane_redis_operation_failures_total`、bounded reason log、alerts、Runbook | Workers Logs／traces、D1 query latency／errors、Queue operation outcomes | event ID、transition、attempt、stable reasonだけをlog | sampling、retention、unsafe exception redaction、cross-product query、on-call routing |
| latency | HTTP、delivery attempt、accepted-to-delivered histogram | Worker request duration、automatic fetch／binding traces、Queue lag、D1 query latency | local state transitionとevent attempt | end-to-end event histogram、SLI window、sampling bias、30日retention |
| event correlation | event ID／request ID logとstatus API | D1 event ID、content-free custom log、Worker trace | same event IDをacceptance、retry、duplicate、terminal stateで維持 | provider aggregate metricから個別eventへ遡るqueryとaccess control |

[Cloudflare Queues metrics](https://developers.cloudflare.com/queues/observability/metrics/)はbacklog、lag、retry、operation outcomeを提供し、[D1 metrics](https://developers.cloudflare.com/d1/observability/metrics-analytics/)はquery volume、row、latency、storageを提供する。[Workers observability](https://developers.cloudflare.com/workers/observability/)はrequest metrics、logs、tracesを提供する。ただしOpenTelemetry exportはlogs／tracesが対象で、metrics exportは現時点で未対応である。このspikeはremote telemetryを有効化せず、既存Prometheus／Grafana／alerts／SLOを完成扱いしない。

## Local reproduction

repository rootで次を実行する。

```bash
make cloudflare-test
make cloudflare-local-flow
make cloudflare-check
make cloudflare-clean-room
```

`cloudflare-test`はprovider-neutral state machine、payload boundary、concurrent idempotency、concurrent repair、retry、DLQ、duplicate boundary、logging safetyを検証する。`cloudflare-local-flow`はtemporary D1とQueueをWrangler／workerdで起動し、既存mock sinkへ実配送した後、今回起動したprocessだけを停止する。Cloudflare account、credential、remote binding、cloud writeは使わない。

`cloudflare-clean-room`はsource-only copyと隔離HOMEでpin付きtoolchain／dependency bootstrapから同じgateを再実行する。GitHub Actionsの専用`cloudflare` jobも`make cloudflare-check`をrequired gateとして直接呼び、Redis/Kubernetesの`quality`と両方が成功してから`e2e-kind`へ進む。これはCI-integrated／local contract validatedの根拠であり、push前のremote Actions成功やCloudflare productionを示さない。

Redis側のcurrent validation入口は`make test-unit`、`make test-integration`、Compose／kind／observability／incident各targetである。Docker／Compose version driftがある環境ではpinを変更せず、runtime系は未確認として区別する。

## Productionへ進む前のgate

- supported payload maximum、413 semantics、streaming／R2再評価を実traffic、plan、memory、cost、abuse protectionから決める
- D1／Queue／Cron／DLQのresource configuration、access、retention、backup、migration rollback、quota、billingをreviewする
- provider DLQ inspection／replayとD1 reconciliation、stale outbox alert、operator approvalを設計する
- existing SLI semanticsをCloudflare telemetryへ実装し、dashboard、alerts、Runbook、notification、retention、redactionを検証する
- Python Workers beta、remote D1／Queues behavior、load、multi-region、real downstream、production SLOを別の明示承認gateで検証する

この文書と[Cloudflare spike](CLOUDFLARE_SPIKE.md)、[ADR 0007](adr/0007-cloudflare-local-delivery-spike.md)はlocal portfolio evidenceであり、deployment recommendationまたはproduction readiness認定ではない。
