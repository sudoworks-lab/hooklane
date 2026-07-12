# Goal Loop 実行プロンプト(1 process / 1 feature)

あなたはrunnerが明示した1件のbounded featureだけを担当する。feature選択と次のiterationの起動は外部runnerの責務である。以下の手順を上から順に、省略せず実行すること。

Goal Loop write iterationはmain agentだけで実行し、Ultraを既定にしない。model / reasoning effortはrunnerを起動する環境の明示設定に従い、このprocess内で変更しない。

## 1. 開始儀式(get up to speed)

1. `pwd` で作業ディレクトリを確認する。
2. `git status --short` を実行し、その出力を開始時の pre-existing dirty 一覧として扱う。
3. docs/RELEASE_EVIDENCE.md を読み、公開済みのtechnical acceptance boundaryを把握する。
4. docs/PLAN.md と docs/features.json を読む。
5. `git log --oneline -20` で直近の作業履歴を確認する。
6. scripts/init.sh があれば実行し、環境を起動する。
7. スモークテスト: PLAN.md の「基本検証」に記載のコマンドを実行する。壊れていたら、新規作業に入らずまず修理する(stop-and-fix)。公開evidenceへ影響する修理だけをRELEASE_EVIDENCEへ反映する。

## 2. 指定タスクの確認

- prompt冒頭の `Runner-assigned bounded feature` にあるfeature IDを確認する。
- featureを自分で再選択しない。指定feature以外へ着手しない。

## 3. 実装

- 指定された1件を、PLAN.md の該当マイルストーンのスコープ内で実装する。
- diff を必要以上に広げない。無関係なリファクタリングをしない。

## 4. 検証

- PLAN.md 該当マイルストーンの検証コマンドをすべて実行する。
- features.json の該当項目の `steps` を実際に実行して確認する。
- 失敗したら直し、直るまで次の手順に進まない。
- **すべて通った場合のみ**、features.json の該当項目を `"passes": true` にし、`"verified_at"` に日時(JST)を記録する。
- 実行した検証コマンドとその結果、および指定featureの状態を応答テキストに要約する。

## 5. 記録(クリーンな終了)

1. 実行履歴ledgerは作成しない。公開済みacceptance factが変わる場合だけ、docs/RELEASE_EVIDENCE.mdを機械検証結果と一致させる。
2. `git status --short` を確認し、開始時の pre-existing dirty 一覧と stage 対象を突き合わせる。
   - pre-existing dirty 一覧に含まれるファイルは stage しない。
   - ただし今回のfeature対応に必要で、明示的に編集した場合は、最終報告に理由を書く。
3. 今回変更・作成したファイルと、変更した場合だけdocs/RELEASE_EVIDENCE.md・docs/features.jsonを、`git add <path> ...` で**明示的に**stageする。
   - `git add -A` および `git add .` の使用は許容されない(無関係な変更・logs/・機密ファイルの巻き込み防止)
   - logs/cache/history/secret/個人情報は commit しない。
4. commit前に `git status --short` と `git diff --cached --stat` を確認する。
5. `git commit -m "<何をなぜ変えたか分かる日本語メッセージ>"`

## 6. Process終了

- 指定featureの実装・検証・evidence記録・状態更新が終わった時点で、未完了の別featureが残っていても最終報告を出してprocessを終了する。
- 別featureを選ばない。同じparent turnを維持しない。次のfeatureは外部runnerが新しいprocessで開始する。
- 全featureがこのprocessの完了時点で検証済みなら `<promise>ALL_FEATURES_PASS</promise>` を出力してよいが、その判定のために別featureを実装してはならない。

## 7. スタックプロトコル

- このprocess内では指定featureについて原因調査、最小修正、再検証を行う。
- 解消できない場合は、試行内容・失敗理由・原因仮説を最終報告に記録し、processを終了する。別featureへ移らない。
- runnerがattempt上限を管理する。agent自身が親turn内でretry loopやGoal Loopを作らない。
- 人間の入力が必要で指定featureを `"blocked": true` にした場合は `<promise>BLOCKED</promise>` を出力して終了する。
