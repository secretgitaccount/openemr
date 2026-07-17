# Clinical Co-Pilot — CI gate (PRP-12, FR-8).
#
# `make ci` runs the PR-blocking gate: lint -> tests+coverage -> eval regression
# gate -> fail-closed PHI scan. It is a *superset* of the local .githooks/pre-push
# hook (which runs the fast `pytest -q` without coverage) — CI additionally
# enforces the coverage floor. Any server-side CI job (on a host that provides
# runners) can call `make ci`; where no runner is available, the local pre-push
# hook (`make install-hooks`) is the PR-blocking gate.
#
# Install the local push hook once:
#   make install-hooks      # == git config core.hooksPath .githooks

AGENT_DIR := agent
PY        := $(AGENT_DIR)/.venv/bin/python
RUFF      := $(AGENT_DIR)/.venv/bin/ruff
COV_MIN   := 80

.PHONY: ci lint test coverage eval-gate phi-check install-hooks update-baseline openapi openapi-check

## Run the full PR-blocking gate. Superset of .githooks/pre-push (+ coverage floor).
ci: lint coverage eval-gate phi-check
	@echo "make ci: all gates green."

## Regenerate the committed OpenAPI 3.1 snapshot (agent/openapi.json).
openapi:
	cd $(AGENT_DIR) && .venv/bin/python -m copilot.scripts.dump_openapi

## Fail if the committed OpenAPI snapshot has drifted from the implementation.
## (Also enforced in the test suite via tests/test_openapi_contract.py.)
openapi-check:
	cd $(AGENT_DIR) && .venv/bin/python -m copilot.scripts.dump_openapi --check

## 1. Lint.
lint:
	@echo "==> ci: ruff check"
	cd $(AGENT_DIR) && .venv/bin/ruff check

## 2. Full test suite (no key, no network) — fast, no coverage (mirrors the hook).
test:
	@echo "==> ci: pytest -q"
	cd $(AGENT_DIR) && .venv/bin/python -m pytest -q

## 2b. Full test suite + coverage floor (the CI test step).
coverage:
	@echo "==> ci: pytest + coverage (fail under $(COV_MIN)%)"
	cd $(AGENT_DIR) && .venv/bin/python -m pytest -q \
		--cov=copilot --cov-report=term-missing --cov-fail-under=$(COV_MIN)

## 3. Golden-set regression gate vs the committed baseline.
eval-gate:
	@echo "==> ci: eval regression gate"
	cd $(AGENT_DIR) && .venv/bin/python -m copilot.evals.gate

## 4. Fail-closed PHI scan of eval artifacts + fixtures.
phi-check:
	@echo "==> ci: PHI check (fail-closed)"
	cd $(AGENT_DIR) && .venv/bin/python -m copilot.scripts.phi_check

## Install the PR-blocking git hook (idempotent).
install-hooks:
	git config core.hooksPath .githooks
	@echo "installed: core.hooksPath -> .githooks (pre-push gate active)"

## Regenerate the committed baseline after an intentional, reviewed change.
update-baseline:
	cd $(AGENT_DIR) && .venv/bin/python -m copilot.evals.gate --update-baseline
