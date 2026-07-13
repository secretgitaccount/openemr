# Clinical Co-Pilot — CI gate (PRP-12, FR-8).
#
# `make ci` mirrors the .githooks/pre-push hook so pull requests are blocked by
# the same four steps that block a local push: lint -> tests -> eval regression
# gate -> fail-closed PHI scan. A GitLab/GitHub CI job simply calls `make ci`.
#
# Install the local push hook once:
#   make install-hooks      # == git config core.hooksPath .githooks

AGENT_DIR := agent
PY        := $(AGENT_DIR)/.venv/bin/python
RUFF      := $(AGENT_DIR)/.venv/bin/ruff

.PHONY: ci lint test eval-gate phi-check install-hooks update-baseline

## Run the full PR-blocking gate (same order as .githooks/pre-push).
ci: lint test eval-gate phi-check
	@echo "make ci: all gates green."

## 1. Lint.
lint:
	@echo "==> ci: ruff check"
	cd $(AGENT_DIR) && .venv/bin/ruff check

## 2. Full test suite (no key, no network).
test:
	@echo "==> ci: pytest -q"
	cd $(AGENT_DIR) && .venv/bin/python -m pytest -q

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
