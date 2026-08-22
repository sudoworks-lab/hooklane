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
make cloudflare-clean-room
```

`cloudflare-test`はPython 3.13 isolated environmentでstate machine、failure injection、configurationを検証する。`cloudflare-local-flow`はtemporary D1 state、Wrangler/workerd、既存Hooklane mock sinkを今回のprocessとして起動し、終了時にそのprocessだけを停止する。使用portが既にlisten中なら既存runtimeへ干渉せずfailする。

Wrangler、Pywrangler、runtime dependencyは`uv.lock`と`pylock.toml`で固定する。Cloudflare Python runtimeで利用可能なPyodide wheelに合わせ、FastAPI 0.141.1とPydantic 2.10.6を使う。既存Hooklane runtimeのFastAPI 0.139.0、Pydantic 2.13.4、Python 3.12は変更しない。

## CI bootstrap

GitHub Actionsの`cloudflare` jobはfixed `ubuntu-24.04`上でNodeを`.nvmrc`の22.22.2から設定し、root `.python-version`のPython 3.12.3だけで`make cloudflare-ci-setup`を開始する。このtargetはSHA-256を固定したPyPI wheelからuv 0.12.3を導入し、root mock sink用のminimal Python 3.12 harnessと、`.python-version`／`pyproject.toml`で`3.13.*`に固定したCloudflare環境を別々に作る。Cloudflare dependencyは`uv sync --locked`、Node dependencyはlocal flow内の`npm ci`で導入する。

`make cloudflare-clean-room`はGit管理対象と未ignoreのsource候補だけを一時directoryへcopyし、一時HOME／cacheで`make cloudflare-ci-check`を実行する。repository内の既存venv、`node_modules`、`python_modules`、`.wrangler`、global uvは使わない。local flow内のnpm、uv、Wrangler、workerdも別の一時HOMEを使い、Cloudflare credential環境を継承せずWrangler telemetryを無効化する。failure時はcontent-freeなsink logを末尾3,000文字、worker logを末尾6,000文字に制限して表示し、payload、raw Idempotency-Key、credentialは出力しない。

workflowへの統合とlocal contractは検証済みだが、追加したCloudflare jobのremote GitHub Actions実行はpush前のため未確認である。

Make／bootstrapのlocal defense-in-depthと、base branch CODEOWNERS／GitHub rulesetによるexternal review boundaryの分離は[`docs/CI_TRUST_MODEL.md`](../docs/CI_TRUST_MODEL.md)に記録する。CODEOWNERS enforcementとrequired checksはGitHub Settingsで未変更・未確認である。

## 保証しないこと

このspikeはproduction readiness、cloud resource configuration、remote D1／Queues、billing、production traffic、Tunnel、DNS、Access、custom domainを検証しない。Cloudflare Python Workersはbetaであり、production payload maximum、Queue ordering、provider telemetry、DLQ operator recoveryも次gateで扱う。Redis backendとの詳細比較は[`docs/REDIS_CLOUDFLARE_COMPARISON.md`](../docs/REDIS_CLOUDFLARE_COMPARISON.md)にある。
