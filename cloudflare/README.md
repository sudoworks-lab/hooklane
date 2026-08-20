# Hooklane Cloudflare local spike

このdirectoryは、既存Redis Streams backendを変更しないlocal-only Cloudflare delivery backendのvertical spikeである。Cloudflare account、credential、remote binding、cloud deploymentは使用しない。

## 構成

- Python WorkerとFastAPI ASGI adapterが`POST /v1/events`、`GET /v1/events/{event_id}`、liveness、dependency readinessを提供する。
- D1が1.5 MB以下のpayload chunk、status、attempt count、canonical request fingerprint、hashed idempotency key、outbox、delivery attempt token／leaseを永続化する。
- Cloudflare Queuesへはpayloadではなく`event_id`だけを送り、consumerがD1からdelivery contentを読む。
- consumerのdelivery destinationは`wrangler.jsonc`の固定mock sink URLであり、request modelにdestinationはない。
- D1 commit後にQueue sendが失敗した場合はpending outboxをscheduled handlerまたはlocal test repair endpointが再送する。repairはD1 compare-and-set leaseで並行ownerを1つに絞り、owner消失時はlease expiry後に同一event IDを再送する。delivery側もD1 token／leaseのCASでstale attemptのstate上書きを防ぎ、delivered／dead_letter duplicateはsinkなしでackする。

`/__spike/*` endpointと`SPIKE_TEST_MODE`はlocal failure evidence専用である。tracked defaultは`SPIKE_TEST_MODE=false`、`workers_dev=false`で、local flowだけがWranglerの明示的`--var SPIKE_TEST_MODE:true`で有効化する。

## Local validation

repository rootから次を実行する。

```bash
make cloudflare-test
make cloudflare-local-flow
make cloudflare-check
```

`cloudflare-test`はPython 3.13 isolated environmentでstate machine、failure injection、configurationを検証する。`cloudflare-local-flow`はtemporary D1 state、Wrangler/workerd、既存Hooklane mock sinkを今回のprocessとして起動し、終了時にそのprocessだけを停止する。使用portが既にlisten中なら既存runtimeへ干渉せずfailする。

Wrangler、Pywrangler、runtime dependencyは`uv.lock`と`pylock.toml`で固定する。Cloudflare Python runtimeで利用可能なPyodide wheelに合わせ、FastAPI 0.141.1とPydantic 2.10.6を使う。既存Hooklane runtimeのFastAPI 0.139.0、Pydantic 2.13.4、Python 3.12は変更しない。

## 保証しないこと

このspikeはproduction readiness、cloud resource configuration、remote D1／Queues、billing、production traffic、Tunnel、DNS、Access、custom domainを検証しない。Cloudflare Python Workersはbetaであり、production payload maximum、Queue ordering、provider telemetry、DLQ operator recoveryも次gateで扱う。Redis backendとの詳細比較は[`docs/REDIS_CLOUDFLARE_COMPARISON.md`](../docs/REDIS_CLOUDFLARE_COMPARISON.md)にある。
