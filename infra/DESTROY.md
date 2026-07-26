# Hooklane AWS destroy手順

この手順は破壊的で、AWS resourceとRedis stateを削除する。自動実行しない。

1. 対象backend、workspace、region、account、state lockを確認する。
2. ALB DNS、ECR repository、CloudWatch log、Secrets Manager、ElastiCache、ECS、VPCが対象であることを確認する。
3. ECR imageを保持する必要がない場合だけ、`ecr_force_delete = true`を明示する。
4. Secret recovery windowとRedis backupの保持判断を行う。
5. destroy planを作成して人間が確認する。

```bash
cd infra
terraform plan -destroy -out=hooklane-destroy.tfplan
terraform show hooklane-destroy.tfplan
terraform apply hooklane-destroy.tfplan
```

このrepositoryのCIはdestroyを実行しない。AWS applyまたはdestroy後は、CloudWatch、ECR、Secrets Manager、ElastiCache、ALB、ECS、VPC endpoint、NAT Gatewayが残っていないことをAWS consoleまたは承認済みread-only queryで確認し、state lockを解放する。
