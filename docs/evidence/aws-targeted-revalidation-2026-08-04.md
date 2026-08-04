# AWS targeted revalidation — tested main commit

## Purpose

This report records a bounded AWS runtime check of the application and runtime source at main commit `b1a73bb2f9b9d71e9cdfbbe96e76a20ee1852b5d`. That commit was main at the time of the AWS test. The AWS deployment used this commit's application/runtime source; this documentation-only commit was not redeployed or revalidated in AWS, and later main commits are outside this evidence. It is a sanitized portfolio artifact derived from the independently verified run evidence and contains no AWS account or resource identifiers.

## Tested source

- Branch at test time: `main`
- Source commit: `b1a73bb2f9b9d71e9cdfbbe96e76a20ee1852b5d`
- Immutable application image tag: `git-b1a73bb2f9b9d71e9cdfbbe96e76a20ee1852b5d`
- Repository state at the run boundary: clean and synchronized with `origin/main`

## AWS topology

The test deployed one API, one worker, and one controlled mock-sink task on ECS Fargate. The stack also used an application load balancer, managed Valkey, private task networking, IAM, Secrets Manager, CloudWatch Logs, service discovery, and private DNS integration.

The runtime used a single desired task for each workload, no NAT Gateway, no multi-AZ cache configuration, no public ECS task IP, and no external downstream.

## Acceptance criteria

The bounded acceptance scope was:

- all three ECS tasks and the load-balancer target become healthy;
- a synthetic API request is accepted and eventually delivered;
- H-01 localizes a poisoned retry member while preserving terminal state;
- H-02 quarantines a malformed pending message without forwarding it;
- a normal event remains deliverable after each targeted failure case; and
- all charge-heavy runtime resources are removed after validation.

## Deployment result

PASS. The API, worker, and mock-sink each ran as one healthy ECS Fargate task, and the application load-balancer target became healthy. Managed Valkey reported version 7.2.4 during the probe.

## Normal delivery result

PASS. The synthetic request received HTTP `202 Accepted`, reached eventual status `delivered`, and recorded at least one delivery attempt.

## H-01 poisoned retry result

PASS. A stale retry member was removed in a bounded operation. The existing terminal status was preserved, the worker loop continued, and a following normal event was delivered. The emitted metric and log event used bounded reason codes and did not expose payload or caller key values.

## H-02 malformed pending result

PASS. A malformed pending message was not sent downstream. It was quarantined with metadata only, the pending count returned to zero, and the associated terminal state was classified as `invalid_message`. Logs did not contain the raw payload or an Idempotency-Key value.

## Post-failure delivery

PASS. A normal event was delivered after H-01 and again after H-02, demonstrating that either localized failure did not stop subsequent worker progress.

## Cleanup audit

PASS. Runtime services were scaled to zero before the artifact-stage cleanup. The final attestation showed:

- Terraform managed resources: 6
- ECR repositories: 3
- ECR lifecycle policies: 3
- images for the tested source commit: 3
- active ECS services: 0
- running ECS tasks: 0
- pending ECS tasks: 0
- application load balancers: 0
- managed Valkey resources: 0
- NAT Gateways: 0
- VPC endpoints: 0
- Hooklane VPCs: 0
- CloudWatch log groups: 0
- Secrets Manager secrets: 0
- Cloud Map namespaces: 0
- Route 53 private hosted zones: 0
- Terraform apply processes: 0

Inactive ECS tombstones may remain as non-runtime history; they were not treated as active or charge-heavy resources.

## Intentionally retained resources

The artifact stage intentionally retained three immutable ECR repositories, 27 total images, and three images for the tested source commit. Approximate compressed image storage was 459,999,829 bytes.

## Cost boundary

Charge-heavy runtime resources were zero after cleanup. Retained ECR image storage is not a complete cost-zero state: the retained compressed storage may incur a small ECR storage charge. The byte count is an evidence count, not a billing estimate.

## Evidence provenance

The primary run summary was independently verified with SHA-256 before this document was created. Its SHA-256 is:

`d0bec4e23b0c2af0ad3965ea34290d3a81fbb8d9944fbda52b6eb334f39fd94b`

Companion artifacts:

- [sanitized machine-readable evidence](aws-targeted-revalidation-2026-08-04.json)
- [public audit counts](aws-targeted-revalidation-2026-08-04.audit.txt)

The mutation duration was 2,107.888 seconds. Repository source, Terraform source, and the primary run evidence were not changed by this documentation work.

## Limitations

This run did not verify the full idempotency matrix, full retry/pending/dead-letter matrix, soak behavior, high availability, autoscaling, rollback, an external downstream, or production certification. Historical identity-fingerprint provenance was intentionally outside this run's decision boundary.

## Verdict

`AWS_TARGETED_TEST_PASS_AND_CLEAN`
