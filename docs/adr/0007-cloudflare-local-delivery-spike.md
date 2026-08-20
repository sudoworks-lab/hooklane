# ADR 0007: Cloudflare local delivery spikeを分離する

- Status: Accepted for portfolio-complete local spike
- Date: 2026-08-20

## Context

Hooklane v0.1.1はPython 3.12、FastAPI、Redis Streamsを正本runtimeとし、外部API、idempotency、retry、dead-letter、at-least-onceのcontractを固定している。Cloudflare-native backendは現行F001〜F029に含まれず、Cloudflare account、credential、resourceは未確認である。

Cloudflare Workers、D1、Queuesへ同じcontractを写像する場合、D1 transactionとQueue sendを単一transactionにできない。Queueへpayloadを直接入れると1 message 128 KB未満の上限が現行APIの無制限payload modelと衝突する。Python WorkersはFastAPIを実行できる一方、Python 3.13以上を要求するbeta機能であり、既存runtimeのPython 3.12契約とは分離が必要である。

## Decision

- `cloudflare/`をlocal-only spikeとして追加し、既存Redis backend、default application、Docker、Compose、Kubernetes、Helmのruntime contractは変更しない。
- ingressはPython WorkerのASGI adapter上でFastAPIを使い、外部の`POST /v1/events`、`GET /v1/events/{event_id}`、health semanticsを再現する。Cloudflare側toolchainはPython 3.13以上として隔離する。
- D1にevent metadata、1.5 MB以下のUTF-8 payload chunk、status、attempt count、canonical request fingerprint、hashed idempotency key、outbox state、delivery token／leaseを保持する。raw Idempotency-Keyは保存しない。
- event row、全payload chunk、outbox rowの作成はD1 batch transactionで行う。このdurable commitが成功するまで202を返さない。idempotencyはD1のunique constraintと同一transaction内のlookupでatomicに判定する。
- Queueへ送るmessageは`event_id`だけとし、consumerがD1からpayload chunkを順に読み復元する。これにより128 KB Queue message limitとD1 2 MB single-row limitをpayload contractから切り離す。
- D1 commit後のQueue send失敗はpending outboxを残してrepair対象にする。repairはD1のatomic compare-and-setでrandom claim tokenと30秒leaseを取得する。同時repairは同じ有効leaseをclaimせず、Queue failure時はclaimをreleaseする。
- Queue send成功後のoutbox transition失敗またはowner消失はlease expiry後に再送し得るため、同一`event_id`のduplicateを許容する。event lossを避け、exactly-onceは主張しない。
- delivery開始はqueued／retry_scheduledまたは期限切れdeliveringだけをD1 CASでclaimし、active ownerをtoken／leaseで識別する。delivered／dead_letter duplicateはsinkなしでackし、stale attemptのretry／success／dead-letter transitionはtoken CAS失敗としてcurrent stateを維持する。attempt 6以降は生成しない。
- consumerはdownstream HTTP 2xxだけをsuccessとし、429、5xx、timeout、connection failureをbounded retry、3xxとその他4xxをdead-letterにする。downstream success後、D1 delivered transitionまたはQueue completion前に失敗した場合は同一`event_id`を再配送し得る。
- delivery destinationはoperator-controlledな固定mock sink bindingだけとし、requestから指定させない。
- local debug signalは`event_id`、state transition、attempt、content-free reason codeに限定する。payload、raw idempotency key、credential、unsafe exception textは出力しない。

## Rejected for this spike

- Durable Objectsは採用しない。D1 unique constraintとtransactional state machineでconcurrent idempotency contractを満たせるかを先にtestする。
- R2は採用しない。Queueにはconstant-sizeのevent referenceだけを送り、D1 payload chunkをacceptance transactionへ含めることで2 MB single-row gapを解消できる。R2 writeとD1 acceptanceのcross-service gap、orphan cleanup、retention、authorization、costを追加する必要性がない。production supported maximumがD1 chunk designと両立しない場合に再評価する。
- Tunnel、DNS、Access、production domain、cloud deploymentは扱わない。

## Consequences and gates

- Python Workers betaとPython version差はproduction readiness gapである。
- D1 single-database throughput、query-per-invocation／database size、Workers request／memory、Queue ordering、actual Cloudflare telemetry、cloud resource configuration、billing、production trafficはこのlocal spikeでは保証しない。
- production候補に進めるには、local failure evidenceに加えてremote-free configuration review、supported payload maximum、stale outbox alert、DLQ recovery／D1 reconciliation、Cloudflare-native telemetry、SLO、security/access boundaryを別gateで決定する。
- Redis backendとのprimitive、DLQ、observability、operational trade-offは[比較資料](../REDIS_CLOUDFLARE_COMPARISON.md)へ固定する。
