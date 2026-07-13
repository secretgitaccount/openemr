# PRP-12 — PR-blocking git hook + PHI check ⭐ GRADED GATE

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-11 · **Blocks:** — · **Needs key:** no

## Goal (atomic)
The **hard gate graders test**: a PR-blocking hook that runs the eval suite +
tests + PHI scan and **fails the build** if any rubric category regresses >5%
vs a committed baseline or drops below its pass threshold (FR-8). During grading
a small regression is injected and the gate must go red.

## Context / files owned
- `.githooks/pre-push`, `agent/src/copilot/scripts/phi_check.py`,
  `tests/eval/golden/baseline.json` (committed baseline), CI wiring
  (`.gitlab-ci.yml` job or a `Makefile`/`make ci` target), install docs.

## Contract
- `pre-push` runs, in order: `ruff check` → `pytest -q` → `python -m
  copilot.evals.w2_runner` → `python -m copilot.scripts.phi_check`. Any nonzero
  → push blocked.
- Regression logic: compare runner output to `baseline.json`; **fail** if any
  category drops >5% absolute **or** below its pass threshold. Print the offending
  category + old→new numbers.
- `phi_check.py`: scan logs/traces/eval artifacts/fixtures for PHI patterns
  (SSN, MRN, DOB, names outside the synthetic allowlist); **fail closed**.
- Install: `git config core.hooksPath .githooks` (documented).

## Validation (must PROVE the gate blocks)
```bash
cd agent && . .venv/bin/activate
python -m copilot.evals.w2_runner            # green baseline
# inject a regression, confirm RED:
#   (temporarily break an evaluator input so factually_consistent drops >5%)
git push  # -> hook exits nonzero, push blocked
# revert -> green again
echo "123-45-6789" >> /tmp/fake.log && python -m copilot.scripts.phi_check /tmp/fake.log  # -> nonzero
```
- **Demonstrated regression → red**, revert → green (paste both outputs).
- PHI check fails on a planted SSN and passes on clean synthetic data.
- Hook is idempotent + documented; CI job mirrors the hook so it also blocks PRs.

## Builder prompt (backend-dev → qa)
> Implement the PR-blocking gate. Write `.githooks/pre-push` running ruff →
> pytest → `w2_runner` → `phi_check`, blocking on any nonzero. Add regression
> logic comparing the runner to a committed `tests/eval/golden/baseline.json`,
> failing if any rubric category drops >5% absolute or below threshold, printing
> the offending category and old→new numbers. Write `scripts/phi_check.py` that
> scans logs/traces/eval artifacts for PHI patterns and fails closed. Wire an
> equivalent CI job. Document `git config core.hooksPath .githooks`. **Prove it:**
> inject a regression and show the hook goes red, revert and show green; show the
> PHI check failing on a planted SSN. Hand to qa — QA must independently reproduce
> the red-on-regression.
