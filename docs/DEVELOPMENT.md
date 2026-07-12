# 開発と検証方法

## 位置づけ

この文書はHooklaneの開発工程を扱う。Webhook配送機能やruntimeの説明は[README](../README.md)と[アーキテクチャ](ARCHITECTURE.md)を正本とする。

## Goal Loop

Goal Loopは、featureごとの実装と検証を分離するためのrepository内runner。正本は[docs/GOAL.md](GOAL.md)、進行状態は[docs/features.json](features.json)、実行規則は[AGENTS.md](../AGENTS.md)と[PROMPT.md](../PROMPT.md)にある。

- 外部runnerは未完了featureを1件だけ選び、専用の新しいprocessを起動する
- 1 processは1 featureだけを扱う。次のfeatureは別processで開始する
- write iterationはmain agentだけが行う。subagent、agent delegation、`/goal`は既定で使わない
- runnerはfeatureごとのattempt上限、wall-clock timeout、timeout receipt、process group終了を管理する
- Python 3がない環境ではshell fallbackへ縮退せずfail-closedとする

## runner command

```bash
bash scripts/loop.sh codex 1 --status
bash scripts/loop.sh codex 1 --dry-run
bash scripts/loop.sh codex 1 --iteration-timeout 1800
```

runnerのsafety contractは[tests/test_loop_runner.py](../tests/test_loop_runner.py)と[tests/test_goal_loop_safety.py](../tests/test_goal_loop_safety.py)で検証する。

## 通常の開発検証

```bash
bash scripts/init.sh
make docs-check
make lint
make typecheck
make test
make verify
```

runtimeを伴う検証、cleanup、公開前の確認は[運用](OPERATIONS.md)を参照する。
