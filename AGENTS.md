# プロジェクト恒久ルール(Goal Loop)

このファイルはエージェント(Codex / Claude Code)向けの恒久ルール。タスク固有の内容は書かない。

## ドキュメント構造

| ファイル | 役割 | エージェントの編集権限 |
|---|---|---|
| docs/GOAL.md | spec(目的・制約・Done when) | 読み取り専用。編集禁止 |
| docs/PLAN.md | マイルストーン・受け入れ基準・検証コマンド | 「判断メモ」セクションへの追記のみ可 |
| docs/RELEASE_EVIDENCE.md | 公開可能なtechnical acceptance summary | 機械検証結果と一致する範囲だけ更新可 |
| docs/features.json | 完了チェックリスト | passes / verified_at / blocked のみ変更可。項目の追加・削除・description変更は禁止 |
| PROMPT.md | ループ実行手順 | 編集禁止 |

## 作業ルール

1. 作業開始前に必ず docs/RELEASE_EVIDENCE.md、docs/PLAN.md、git log(直近20件)を読むこと。
2. 外部runnerが指定したfeature 1件だけを1 process / 1 parent turnで扱う。featureを自分で選び直さず、完了後に別featureへ進まない。
3. 検証(docs/PLAN.md 記載のコマンド)に通っていない項目の passes を true にすることは許容されない。
4. 検証が失敗したら、新規作業に進まず修理を優先すること(stop-and-fix)。
5. 毎周の終わりに必ず、公開evidenceへ影響する場合だけdocs/RELEASE_EVIDENCE.mdを検証結果に合わせ、説明的なメッセージでgit commitする。実行履歴ledgerを追加しない。
6. git のstageは変更したファイルの明示指定のみ。`git add -A` / `git add .` は使用禁止(無関係な変更・logs/・機密ファイルの巻き込み防止)。
7. 作業開始前に `git status --short` を確認し、開始時のdirty一覧を pre-existing dirty として扱う。
8. pre-existing dirty は保護対象。今回の作業と無関係に編集・stage・commitしない。
9. stage前に、開始時dirty一覧とstage対象を突き合わせる。pre-existing dirtyを今回の作業で明示的に編集する必要がある場合は、最終報告に理由を書く。
10. logs/cache/history/secret/個人情報はcommit禁止。
11. features.json の項目を削除・編集することは、機能の欠落やバグの見逃しにつながるため許容されない。
12. Goal Loopのwrite iterationはmain agentだけで実行する。subagent、agent delegation、`/goal`、`wait_agent`を使用せず、次のfeatureは外部runnerが新しいprocessで開始する。
13. 「完走」「止まらない」は指定された1 featureの範囲にだけ適用し、プロジェクト全体を同一turnで処理する意味には拡張しない。

## 禁止事項

- ファイル・ブランチの削除、git push --force、git reset --hard(明示指示がある場合を除く)
- docs/GOAL.md の編集
- 検証をスキップした passes: true への変更
- PLAN.md にないタスクの追加実装
- logs/ 配下のファイルのcommit

## 言語

- ドキュメント・コミットメッセージは日本語。コード・コマンド・技術用語は英語のまま。
