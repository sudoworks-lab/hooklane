# キックオフ(initializer)プロンプト — 初回に1度だけ実行

あなたはこのプロジェクトのGoal loopの初期化(initializer)を担当する。**実装はまだ始めないこと。** 以下を順に実行する。

## 1. specの読み込み

- 初期化前に `git status --short` を確認し、その出力を pre-existing dirty として記録する。未初期化で失敗した場合は、その事実を pre-existing dirty 不明として記録する。
- 導入先に既存の `AGENTS.md` / `CLAUDE.md` / `.gitignore` / `PROMPT.md` がある場合は上書きせず、人間にマージ判断を求める。
- docs/GOAL.md を読む。不明点・曖昧な点があれば、作業を始める前に箇条書きで質問し、回答を待つこと(曖昧なまま計画を作らない)。

## 2. features.json の生成

- GOAL.md の要件を、検証可能な機能単位に展開して docs/features.json を生成する。
- 各項目のフォーマットは既存の docs/features.json の `_format` 定義に従う。
- description は「〜できる」「〜が存在する」のような検証可能な文にする。「良い」「使いやすい」のような主観語は使わない。
- steps には検証の具体手順(実行するコマンド、確認する内容)を書く。
- 全項目 `"passes": false` で初期化する。
- 主観評価が必要な要件は features.json に入れず、PLAN.md の「人間レビュー項目」に分離する。

## 3. PLAN.md の生成

- docs/PLAN.md のテンプレート構造に従い、以下を書く:
  - マイルストーン一覧。各マイルストーンは**ループ1周(1セッション)で完了する粒度**まで分割する
  - 各マイルストーンの受け入れ基準と、実行可能な検証コマンド
  - 基本検証(毎周のスモークテストで回すコマンド)
  - 想定アーキテクチャ・技術選定とその理由

## 4. init.sh の生成

- 環境のセットアップ・起動・基本検証を1コマンドで行える scripts/init.sh を書く。
- 依存のインストール、サーバやツールの起動、基本検証の実行を含める。

## 5. 初期化の完了

1. `git init`(未初期化の場合)
2. docs/RELEASE_EVIDENCE.mdを作成し、生成したfeature contractと未検証状態を公開可能な範囲で記録する。未実行の検証をpassと記載しない。
3. 生成・更新したファイルだけを `git add <path> ...` で**明示的に**stageし、`git commit -m "chore: Goal loop環境の初期化"` する。
   - `git add -A` および `git add .` の使用は許容されない(既存の無関係な変更・logs/・機密ファイルの巻き込み防止)
   - 既存dirtyがある場合は、Goal Loop初期化で生成・更新したファイルだけを明示stageし、pre-existing dirty を初期化commitに混ぜない。
   - pre-existing dirtyを初期化に必要なファイルとして編集した場合は、最終報告に理由を書く。
   - logs/cache/history/secret/個人情報は commit しない。
   - commit前に `git status --short` と `git diff --cached --stat` を確認する。
4. 生成した PLAN.md と features.json の要約を提示し、人間のレビューを求めて終了する。**レビュー承認前にループを開始しないこと。**
