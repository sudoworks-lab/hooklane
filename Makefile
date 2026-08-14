PYTHON ?= $(shell command -v python 2>/dev/null || command -v python3 2>/dev/null)
TEST_PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,$(PYTHON))
IMAGE_TAG ?= 0.1.1
export IMAGE_TAG
API_IMAGE := hooklane-api:$(IMAGE_TAG)
WORKER_IMAGE := hooklane-worker:$(IMAGE_TAG)
MOCK_SINK_IMAGE := hooklane-mock-sink:$(IMAGE_TAG)
PROMETHEUS_IMAGE := prom/prometheus@sha256:f39df5334dee301b885f77e0ff1159f5d8a43bf9db518f885544594799a1e3c2
GRAFANA_IMAGE := grafana/grafana@sha256:5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2
TARGET ?= image
QUALITY_PATHS := src scripts tests
UNIT_TESTS := tests/unit tests/test_loop_runner.py tests/test_goal_loop_safety.py

.PHONY: doctor smoke-fast lint typecheck test-unit test-integration test verify \
	security security-secret security-dependency security-filesystem security-image \
	docs-core-check docs-check repository-hygiene-check release-readiness-check \
	demo-smoke clean-room runtime-hygiene-check final-audit \
	ci-setup ci-contract \
	images-build image-contract \
	container-policy-check env-example-check compose-up smoke e2e-local compose-down \
	kind-config-check chart-validate-base chart-validate cluster-up deploy diagnostics \
	chart-smoke resiliency-smoke e2e-kind rollout-smoke cluster-down \
	observability-images observability-validate observability-up \
	observability-smoke-base observability-down alert-rules-check observability-smoke \
	incident-downstream-5xx incident-redis-outage incident-worker-stop incident-smoke \
	terraform-validate worker-ecs-health-repro

doctor:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/doctor.py

smoke-fast:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/smoke_fast.py

ci-setup:
	@test -n "$(PYTHON)" || { echo "[fail] Python: no CI setup interpreter was found"; exit 1; }
	@$(PYTHON) scripts/ci_setup.py

lint:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no lint interpreter was found"; exit 1; }
	@$(TEST_PYTHON) -m ruff check $(or $(LINT_PATHS),$(QUALITY_PATHS))

typecheck:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no typecheck interpreter was found"; exit 1; }
	@$(TEST_PYTHON) -m mypy $(or $(TYPECHECK_PATHS),$(QUALITY_PATHS))

test-unit:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no test interpreter was found"; exit 1; }
	@echo "[test] unit"
	@PYTHONPATH=src $(TEST_PYTHON) -m pytest $(or $(TESTS),$(UNIT_TESTS))

test-integration:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no test interpreter was found"; exit 1; }
	@echo "[test] integration"
	@PYTHONPATH=src $(TEST_PYTHON) scripts/run_integration_tests.py \
		$(or $(TESTS),tests/integration)

test: test-unit test-integration

docs-core-check:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no docs interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/docs_core_check.py

repository-hygiene-check:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no hygiene interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/repository_hygiene.py

docs-check: docs-core-check env-example-check ci-contract repository-hygiene-check
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no docs interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/docs_check.py

demo-smoke: docs-check
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no demo interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/demo_smoke.py

runtime-hygiene-check:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no runtime hygiene interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/clean_room.py --runtime-only

clean-room: docs-check
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no clean-room interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/clean_room.py

release-readiness-check: docs-check
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no release audit interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/repository_hygiene.py --require-complete --require-clean

final-audit: docs-check security-secret release-readiness-check runtime-hygiene-check

security-secret:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no security interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/security_gate.py secret

security-dependency:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no security interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/security_gate.py dependency

security-filesystem:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no security interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/security_gate.py filesystem

security-image: images-build
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no security interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/security_gate.py image --image-tag "$(IMAGE_TAG)"

security: images-build
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no security interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/security_gate.py all --image-tag "$(IMAGE_TAG)"

terraform-validate:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/terraform_contract.py

worker-ecs-health-repro:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@docker build --target worker --tag $(WORKER_IMAGE) .
	@$(PYTHON) scripts/worker_ecs_health_reproduction.py

verify: smoke-fast lint typecheck test security chart-validate docs-check terraform-validate

ci-contract:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no CI contract interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/ci_contract.py

images-build:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/image_tag.py --image-tag "$(IMAGE_TAG)"
	@docker build --target api --tag "$(API_IMAGE)" .
	@docker build --target worker --tag "$(WORKER_IMAGE)" .
	@docker build --target mock-sink --tag "$(MOCK_SINK_IMAGE)" .

image-contract:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/image_contract.py --image-tag "$(IMAGE_TAG)"

container-policy-check:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/container_policy_check.py --target $(TARGET) --image-tag "$(IMAGE_TAG)"

env-example-check:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/env_example_check.py

compose-up: images-build
	@docker compose up --detach --wait --wait-timeout 120

smoke:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/local_e2e.py smoke

e2e-local:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no test interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/local_e2e.py idempotency
	@HOOKLANE_TEST_REDIS_URL=redis://127.0.0.1:16379/15 PYTHONPATH=src \
		$(TEST_PYTHON) -m pytest \
		tests/integration/test_retry_delivery.py \
		tests/integration/test_dead_letter.py \
		tests/integration/test_pending_recovery.py

compose-down:
	@docker compose down --volumes --remove-orphans --timeout 15

kind-config-check:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/kind_config_check.py

chart-validate-base:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no test interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/chart_validate_base.py
	@$(TEST_PYTHON) -m pytest tests/unit/test_chart_base_contract.py

chart-validate:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no test interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/chart_validate.py
	@$(TEST_PYTHON) -m pytest tests/unit/test_helm_policy_contract.py

cluster-up:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/kind_runtime.py cluster-up

deploy: images-build
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/kind_runtime.py deploy

diagnostics:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/kind_runtime.py diagnostics

chart-smoke:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/kind_runtime.py helm-test
	@$(PYTHON) scripts/chart_smoke.py

resiliency-smoke:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/kubernetes_resiliency.py

e2e-kind: images-build observability-images chart-validate
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/kind_e2e.py

rollout-smoke: chart-validate
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/rollout_smoke.py

cluster-down:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/kind_runtime.py cluster-down

observability-images:
	@docker pull $(PROMETHEUS_IMAGE)
	@docker pull $(GRAFANA_IMAGE)

observability-validate:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no test interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/observability_validate.py

observability-up: images-build observability-images observability-validate
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/observability_runtime.py up

observability-smoke-base: observability-validate
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/observability_runtime.py smoke-base

observability-down:
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/observability_runtime.py down

alert-rules-check:
	@test -n "$(TEST_PYTHON)" || { echo "[fail] Python: no test interpreter was found"; exit 1; }
	@$(TEST_PYTHON) scripts/alert_rules_check.py

observability-smoke: observability-validate alert-rules-check
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/observability_runtime.py smoke

incident-downstream-5xx: images-build observability-images chart-validate alert-rules-check
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/incident_downstream_5xx.py

incident-redis-outage: images-build observability-images chart-validate alert-rules-check
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@$(PYTHON) scripts/incident_redis_outage.py

incident-worker-stop: images-build observability-images chart-validate alert-rules-check
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@PYTHONPATH=src $(PYTHON) scripts/incident_worker_stop.py $(if $(NORMALIZED_OUTPUT),--normalized-output "$(NORMALIZED_OUTPUT)",)

incident-smoke: images-build observability-images chart-validate alert-rules-check
	@test -n "$(PYTHON)" || { echo "[fail] Python: neither python nor python3 was found"; exit 1; }
	@PYTHONPATH=src $(PYTHON) scripts/incident_smoke.py
	@git diff --check
