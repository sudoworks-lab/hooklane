# ADR 0004: F004配送先contractの一度限りの移行

## 状態

承認済み。2026-07-27にHuman Decisionとして、F004の旧contractから新contractへの一度限りのmigrationを承認した。

## 背景

F004には、`DELIVERY_TARGET_ALLOWLIST`で配送先を制限するという旧表現が残っていた。しかし、このallowlistは実行時に参照されず、実際の実装はproject内mock sinkをdefaultとし、operator-controlledなstartup configurationだけでcontrolled downstreamへ切り替える構成である。requestから配送先を指定できるAPIは存在しない。

旧表現を残すと、実行されていないsecurity controlを実装済みと誤認させるため、feature contractと公開文書の一度限りの移行が必要になった。

## 決定

F004の新contractを次のように固定する。

- project内mock sinkをdefault配送先とする
- controlled downstreamへの切替はoperator-controlledなstartup configurationだけで行う
- requestから配送先を変更できない
- Redis URLとdownstream URLはruntime parserとHelm templateでcredential、query、fragment、whitespaceを拒否する
- credential-bearing Redis URLはKubernetes Secretのname/keyからだけ注入する
- Helm ConfigMapへcredential-bearing Redis URLを出力しない
- `DELIVERY_TARGET_ALLOWLIST`は未使用のため削除する

今回のF004更新はこのcontract migrationだけを対象とする。future agentは、features.jsonのdescription、steps、項目自体を通常編集してはならず、検証結果に対応する`passes`、`verified_at`、`blocked`だけを既存ルールの範囲で更新する。恒久的な自動編集権限の緩和は行わない。

## 検討した代替案

- 未使用allowlistを残して文書だけを修正する案は、実行時に強制されないsecurity境界を示すため不採用とした。
- requestごとの任意URLをallowlistで制限する案は、credential、SSRF、運用者境界を広げるため不採用とした。
- controlled downstreamをdefaultにする案は、mock sinkによる再現性とoperator-controlled boundaryを維持できるため採用した。

## 結果

実装、Helm contract、integration test、GOAL、PLAN、features.json、security/documentation contractを新contractへ一致させた。F004の`verified_at`は、このmigrationを含む完全なlocal verificationの実行時刻へ更新する。AWS runtime evidenceは旧source/image commitに対するsanitized evidenceであり、現在HEADまたは将来の新immutable imageのAWS実証とは扱わない。
