# PRP-11 — 50-case golden set + boolean rubrics + runner

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-10 · **Blocks:** PRP-12 · **Needs key:** no (deterministic evaluators + stubbed model)

## Goal (atomic)
The graded eval dataset (FR-8): 50 synthetic/demo cases exercising extraction,
evidence retrieval, citations, refusals, and missing-data behavior — scored by
**boolean rubrics only**, reproducible from the repo (files, not a DB).

## Context / files owned
- `agent/tests/eval/golden/**` (case files + fixtures), `agent/src/copilot/evals/w2_runner.py`.
- Reuse the Week-1 Langfuse dataset pattern (`evals/langfuse_eval.py`) to publish
  scores, but scoring is deterministic + boolean.

## Contract
- `GoldenCase` — `{id, kind, input (doc ref or query), expected_behavior,
  expected: {schema_valid, citation_present, factually_consistent, safe_refusal,
  no_phi_in_logs}}`.
- Rubric categories (all boolean): `schema_valid`, `citation_present`,
  `factually_consistent`, `safe_refusal`, `no_phi_in_logs`.
- `run_golden() -> EvalReport` — per-case rubric results + per-category pass
  rate; writes a committed `tests/eval/golden/results.json` and (optionally)
  pushes to Langfuse.
- Case mix: ~15 extraction, ~10 evidence, ~10 citation, ~8 refusal/out-of-panel,
  ~7 missing-data. All synthetic.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_golden_runner.py -q && python -m copilot.evals.w2_runner
```
- Exactly 50 cases load; each declares expected values for all five categories.
- Runner scores all 50 **offline** (stubbed model) → per-category pass rate.
- Each rubric evaluator is unit-tested (a passing and a failing example each).
- `no_phi_in_logs` evaluator actually inspects emitted logs for PHI patterns.
- Results are reproducible from the repo alone (no DB dependency).

## Builder prompt (backend-dev → qa)
> Build a 50-case golden set under `tests/eval/golden/` (synthetic cases across
> extraction, evidence retrieval, citations, refusals, missing-data), each
> declaring expected boolean outcomes for `schema_valid, citation_present,
> factually_consistent, safe_refusal, no_phi_in_logs`. Implement
> `evals/w2_runner.py::run_golden()` scoring all 50 offline (stubbed model) into
> per-category pass rates, writing a committed `results.json` and reusing the
> Week-1 Langfuse dataset pattern to publish. Unit-test each rubric evaluator
> (pass + fail example); make `no_phi_in_logs` truly scan logs. Ensure repo-only
> reproducibility. ruff + pytest green. Hand to qa.
