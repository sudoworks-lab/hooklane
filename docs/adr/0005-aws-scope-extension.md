# ADR 0005: Original P0完了後のAWS/Terraform scope extension

## 状態

Status: 承認済み（Approved）

## 背景（Context）

HooklaneのOriginal P0 Goalは、Webhook受付と非同期配送、local Compose、kind／Helm、observability、incident drill、CI品質ゲートを一貫したrepository contractとして完成させることを目的にした。公開文書には、このP0の後にAWS/Terraformの限定vertical sliceが追加された経緯と境界を明記する必要がある。

## Original P0 boundary

[`docs/GOAL.md`](../GOAL.md)のOriginal P0では、Terraformとcloud deploymentをNon-goalとしていた。これはP0時点のscopeであり、P0完了後の拡張を否定する実装矛盾ではない。Original P0 Goal本文は歴史的な設計入力として保持し、後から最初からAWSがGoalだったように書き換えない。

## 決定（Decision）

Original P0完成後に、AWS/Terraformの限定vertical sliceを追加した。対象はECS/Fargate deployment、security/cost boundary、immutable image、staged apply、cleanupの実証である。この拡張はapplication delivery semanticsを再設計するものではない。

## Why the scope was extended

P0で固定したdelivery contractを保ったまま、cloud側のdeployment boundary、費用の大きいresourceを避けるstage設計、commit固定image、Human approvalを伴うapplyとcleanupの順序を検証可能にするためである。これはP0の受入結果を置き換えず、別の証拠境界を追加する。

## Safety boundaries

- cloud productionの認定ではない
- automatic deploymentを導入しない
- AWS apply、image push、cleanupはHuman approval boundaryとする
- application delivery semantics、retry／DLQ／pending recoveryのremote実証を追加しない
- 実行時はcommit固定image、staged apply、最終cleanupを要求する

## Evidence boundary

current AWS evidenceのsource commitは`123c00c93125b62c0d2bb6b31afd57d6bc5d4a8b`に限定され、結果は`PASS_AND_CLEAN`である。これはcurrent main自体をAWS-tested sourceとは扱わず、[`docs/aws/runtime-evidence.json`](../aws/runtime-evidence.json)のschema 1.1、run IDs、plan counts、SHA provenanceを変更しない。Hosted CIの成功もcloud productionやAWS runtimeの証拠ではない。

未確認のまま残るのは、long-running stability、HA、pending recovery、retry／DLQ remote injection、automatic rollback、real external downstream、autoscaling、OpenTelemetry／X-Ray、in-flight graceful shutdown、ECR scan severity、外部独立監査である。

## 結果（Consequences）

P0の設計境界と後続AWS/Terraform検証の目的を分離して説明できる。AWSのdeployment、security、cost、image、cleanupに関する限定的な証拠は追加されるが、production運用の認定、長期安定性、可用性保証、delivery semanticsの拡張は追加されない。AWS/Terraformの利用にはHuman reviewと課金・破壊リスクの判断が残る。

## Still non-goals

- cloud production運用、長期負荷、HA、multi-AZ／multi-region
- automatic deployment、automatic rollback、unattended AWS apply／push／cleanup
- real external downstream、remote pending recovery、retry／DLQ fault injection
- application feature追加、delivery semanticsの再設計、authentication／authorization、autoscaling

## 検討した代替案

- Original P0 Goalを書き換えてAWSを当初から含める案は、履歴とscope boundaryを誤るため採用しない。
- AWS scope extensionを説明せずにTerraformだけを追加する案は、P0のNon-goalとの関係が不明になるため採用しない。
- cloud productionを認定する表現は、限定vertical sliceの証拠範囲を超えるため採用しない。
