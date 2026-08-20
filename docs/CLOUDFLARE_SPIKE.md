# Cloudflare-native delivery local spike

## 結論

Cloudflare Python Worker、D1、QueuesでHooklaneの主要delivery contractをlocal emulationへ写像する。Redis backendはdefault implementationとして保持し、Cloudflare側は`cloudflare/`に分離する。D1 event/outbox transactionを202 acceptance boundaryとし、Queue sendとの非原子性はpending outboxのrepairと同一`event_id`の再送でevent lossを避ける。delivery attemptはD1 token／leaseで所有し、terminal duplicateはsinkを再実行せずackする。

これはlocal vertical spikeであり、cloud deployment、production readiness、Cloudflare-native observability完成を主張しない。

## Contract mapping

| Hooklane contract | Cloudflare spike | Local evidence |
|---|---|---|
| `POST /v1/events`はdurable persistence前に202を返さない | D1 batchでeventとoutboxをcommit後に202を返す | D1 persistence faultは503、Queue send faultはpending outbox付き202 |
| same key / same request | hashed keyのD1 unique constraintとcanonical fingerprintで同一`event_id`を返す | sequential reuseと20 concurrent requestで1 logical event、1 enqueue |
| same key / different request | fingerprint mismatchを409にする | local HTTP 409 |
| queued / delivering / retry_scheduled / delivered / dead_letter | D1 status constraintとstate transitionで保持する | core failure testsとlocal normal flow |
| attempt count | consumer delivery開始時にD1でattempt-ownedにclaimする | normal 1、retry後2、maximum attempts 5、terminal duplicateはattempt不変 |
| at-least-once | Queue ack前の失敗は同一`event_id`を再配送し得る。terminal duplicateはsinkなしでackする | delivery transition failureで同一event IDを再配送、terminal／stale raceをlocal D1で確認 |
| retry分類 | timeout、connection、429、5xxをretryし、3xxとその他4xxをdead-letterにする | automated classification、retry、non-retry、maximum attempts tests |
| bounded retry / dead-letter | application maximum attemptsは5。D1 CASでattempt 6を防ぎ、terminal duplicateはQueue `max_retries`を増やさずackする。consumer handler自体の継続failureはprovider DLQへ移る | terminal redelivery、maximum-attempt、stale ownership testsとWrangler configuration contract |
| success前にackしない | D1 delivered transition成功後だけQueue messageをackする | duplicate-boundary failure injection |
| destinationをrequestから指定させない | operator-controlled `MOCK_SINK_URL`だけを使用する | request schema parityとconfiguration contract |
| content-free correlation | event ID、transition、attempt、reason codeだけをJSON logにする | logging safety negative assertions |
| livenessとreadinessを分離 | livenessはdependencyを読まず、readinessだけD1へ`SELECT 1`する | local HTTP 200/200とsource contract |

## D1とQueueのatomicity gap

acceptanceは次の順序で行う。

1. D1 batch transactionがevent rowとpending outbox rowを永続化する。idempotency conflictもこの境界で決まる。
2. D1 commit失敗は503とし、Queue sendを行わない。
3. Queueへ`{"event_id":"..."}`を送る。send失敗でもD1 pending outboxが残るため、durable acceptance済みのeventには202を返せる。
4. Queue send成功後にoutboxをsentへ更新する。この更新が失敗した場合はpendingのままrepairが再送するためduplicateはあり得るが、event lossは避ける。
5. scheduled handlerがpending outboxを再送する。local testでは`POST /__spike/repair`が同じ処理を同期実行する。

repairはD1のatomic `UPDATE ... RETURNING`でrandom claim tokenと30秒leaseを取得し、send成功後のstate transitionはclaim tokenを条件にcompare-and-setする。20 concurrent repairは1 owner、1 Queue send、最終`delivered`となることをunit testとWrangler local flowで確認した。ownerがsend成功後に消失した場合はlease expiry後に再送するため、duplicateはあり得るがevent lossを避ける。production gateではstale outbox alert、lease tuning、operator recoveryを決める。

deliveryもD1のdelivery tokenと30秒leaseを使う。active ownerがいるduplicateはQueue retryへ戻し、lease expiry後だけ新しいownerがclaimする。新しいownerのsuccess後に古いownerがfailureまたはsuccessを完了しても、token CASが失敗するためcurrent stateを上書きしない。`delivered`と`dead_letter`はterminalとして再配送時のsink invocationとattempt incrementを抑止する。exactly-onceは主張せず、state transition／ack前のfailureでは同一event IDの再配送を許容する。

## Payload-size decision

Cloudflare Queuesはmessageが128 KB未満でなければならない。現行`EventRequest`はpayload byte sizeを制限していない。spikeはQueueへconstant-sizeのevent referenceだけを送り、128 KBを超えるdirect envelope相当のpayloadもlocal flowでdeliveryできることを確認する。

payload本体を置くD1にはstring、BLOB、rowが2,000,000 bytesまでという上限がある。canonical payload JSONをUTF-8境界で1,500,000 bytes以下の`event_payload_chunks` rowへ分割し、event、全chunk、outboxと同じD1 batch transactionでcommitする。これにより新しいAPI limitを加えずD1 single-row gapを解消した。Wrangler local flowでは2,065,536-byte payloadを`delivered`まで確認した。

Cloudflare platform全体としてpayload無制限を保証するものではない。Workers request／memory、D1 query-per-invocation／database sizeが残るため、production supported maximumと413 semanticsは次gateで決める。比較と選択理由は[Redis StreamsとCloudflareの比較](REDIS_CLOUDFLARE_COMPARISON.md#payload-contractの決定)を正本とする。

## Durable ObjectsとR2

- Durable Objectsは不採用。D1 unique constraintとtransactional acceptanceをlocal concurrent testで検証できたため、idempotency coordinationだけを理由に追加しない。
- R2は不採用。Queue messageをevent referenceに限定し、payloadはtransactional D1 chunk rowで2 MB single-row gapを解消した。R2 writeとD1 acceptanceのcross-service atomicity、orphan cleanup、retention、costを追加する根拠がない。production supported maximumがD1 chunk designと両立しない場合だけ再評価する。

## DLQ and recovery semantics

non-retryable downstream responseとmaximum attemptsはD1 `dead_letter`をcanonical terminal stateとし、不要なQueue retryを行わずackする。Poison message、D1 unavailable、consumer exceptionのようにhandlerが完了できないfailureはQueuesの`max_retries=4`とprovider DLQへ委ねる。

Redis dead-letter streamのinspection／recovery commandとCloudflare provider DLQのoperator workflowは同値ではない。provider DLQのinspection、replay、retention、D1 terminal stateとのreconciliationは未実装であり、次gateに残る。

## Observability difference

local Workerは次のcontent-free fieldだけをstructured JSONとして出力する。

- `event_id`
- `transition`
- `attempt`
- stable `reason`

payload、raw Idempotency-Key、credential、downstream exception textは出力しない。既存Prometheus metrics、Grafana dashboard、alerts、SLI、SLO、RunbookはRedis runtime向けであり、このspikeではCloudflare-native telemetryへ置換していない。request、backlog、delivery outcome、retry、dead-letter、failure diagnosis、latency、event correlationの対応とgapは[比較資料](REDIS_CLOUDFLARE_COMPARISON.md#observability-semantic-mapping)へ固定する。

## Automated failure evidence

`make cloudflare-test`は次を検証する。

1. normal deliveryの全state transition
2. idempotent reuseとduplicate enqueueなし
3. concurrent same-key acceptanceが1 logical event
4. idempotency conflict
5. retryable 5xx後の同一event delivery
6. non-retryable 4xxの即時dead-letter
7. maximum attemptsのterminal dead-letter
8. downstream success後のstate failureによるduplicate boundary
9. D1 persistence failureとpayload chunk transaction failure時のfalse 202禁止
10. Queue send failure後のpending outbox repair
11. Queue send成功後のtransition failureとloss-safe duplicate
12. Queue 128 KB boundaryとevent-reference message
13. D1 2 MBを超えるpayloadのUTF-8 chunk分割と復元
14. 20 concurrent outbox repairのsingle claimとevent lossなし
15. stale lease expiry後のat-least-once repair
16. statusとattempt count parity
17. content-free logging safety
18. dead-letter terminal redeliveryのsink抑止
19. delivered terminal redeliveryのsink抑止
20. newer successとstale failure／successのownership CAS
21. delivery transition failure後のat-least-once duplicate boundary
22. production `JsonTrace`のcontent-free出力

`make cloudflare-local-flow`はWrangler 4.124.0/workerdのlocal D1とQueue、既存mock sinkを用い、tracked defaultから明示的にtest modeをoverrideしてHTTP 202/404/409/503、normal delivery、20 concurrent idempotency、20 concurrent outbox repair、outbox duplicate suppression、D1 single-row limitを超えるpayload delivery、mock sink 503継続時のattempt 5 terminal dead-letter、terminal redelivery、concurrent stale ownership、delivery transition failure boundaryを検証する。

## Next gate

portfolio spikeではD1 payload chunking、outbox lease、DLQ二層差、observability semantic mappingまでを確定した。productionへ進む前に、supported payload maximum、provider DLQのreplay／D1 reconciliation、Cloudflare-native telemetry実装、Python Workers beta許容、remote resource configuration／security／costを決定する。remote applyやdeploymentは別の明示承認が必要である。
