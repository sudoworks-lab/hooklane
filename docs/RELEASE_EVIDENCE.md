# Hooklane v0.1 release evidence

## Scope

This document records the technical acceptance boundary for the Hooklane v0.1 source snapshot. It summarizes reproducible repository contracts and local verification; it is not an operational history or a production certification.

## Feature acceptance

- F001 through F029 are verified in `features.json`: 29/29 have `passes: true`.
- Blocked feature count is 0.
- Feature descriptions and verification steps remain the machine-readable acceptance contract.

## Quality gate

`make verify` is the aggregate quality gate. It runs syntax and configuration checks, Ruff, strict mypy, unit and integration suites, security scans, Helm and Kubernetes validation, and documentation contracts. Acceptance requires every constituent command to exit successfully.

## Runtime verification

### Compose demo

`make demo-smoke` verifies local image build, service health, event acceptance, asynchronous delivery, status lookup, metrics exposure, and project-specific cleanup.

### kind E2E

`make e2e-kind` verifies the pinned kind and Helm topology, normal delivery, idempotency, retry, pending recovery, status lookup, and cleanup.

### Rollout and rollback

`make rollout-smoke` verifies an available rolling update, bounded worker drain, rejection of an intentionally bad revision, explicit Helm rollback, and recovery.

### Observability smoke

`make observability-smoke` verifies Prometheus targets and metrics, Grafana provisioning, SLI queries, alert rules, Runbook references, injected failure signals, and recovery.

### Incident smoke

`make incident-smoke` verifies the downstream 5xx, Redis outage, and worker-stop scenarios, including detection, recovery, and accepted-event accounting.

### Clean-room verification

`make clean-room` reconstructs a disposable candidate from tracked Git state without hardlinks, then runs initialization, dependency setup, verify, demo, kind E2E, rollout, observability, incident, documentation, diff, and cleanup contracts.

## Security scanning

The accepted local gate reported:

- Gitleaks: no secret findings in the scanned Git history or working tree.
- OSV-Scanner: no known vulnerabilities in `requirements.lock`.
- Trivy filesystem and the locally built API, worker, and mock-sink images: no HIGH or CRITICAL findings under the repository policy.

Scanner databases and upstream advisories change. These results describe the verified snapshot and must be rerun for later revisions.

## Verified facts

- The local mechanical quality, security, documentation, Helm, Compose, kind, rollout, observability, and incident contracts passed.
- Delivery is at-least-once and can produce duplicate downstream attempts.
- The verified topology is local, single-node kind with a single Redis instance and the repository mock sink.
- Runtime verification uses locally built application images and pinned upstream images.

## Not verified

- GitHub hosted Actions has not been executed.
- Cloud production, external downstream systems, multi-node or multi-zone availability, long-running load, and production traffic have not been verified.
- The SLO is a design target, not evidence of rolling 30-day attainment.
- This evidence is not a claim of production readiness, security certification, or operational experience.

## Distribution boundary

This repository is a source-only distribution. It publishes source code, Dockerfile, Helm chart, configuration, documentation, and validation procedures. It does not provide prebuilt container images, a container registry, release artifacts, or binary distributions. Users build application images locally; upstream licenses and notices continue to apply to third-party dependencies and images.
