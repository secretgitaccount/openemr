# PRP M1-6 · Verification gate (grounding + first deterministic rule)

**Milestone:** M1 · **Depends on:** M1-1, M1-3 · **Blocks:** M1-7 · **Needs API key:** no

## Goal
The trust boundary between the model and the display (FR-8/9/10). Two independent, deterministic (non-LLM) checks:
1. **Grounding** — drop/flag any `Claim` whose `SourceRef`s don't correspond to a record actually in the retrieved set. A claim with no valid source pointer is never rendered as fact.
2. **First domain rule** — **allergy contraindication**: cross-check active meds against `AllergyIntolerance`; if a med matches an allergen, surface a flag **even if the model didn't** (override/annotate).

## Context
- Pure Python, in-process, independent of the model (PRD §12 verification = deterministic engine).
- Inputs: the `GroundedSummary` from M1-5 + the `CriticalSet` from M1-3 (the ground truth of what was actually retrieved).
- Own new files only: `verification/gate.py`, `verification/rules.py`.

## Spec
- `verification/gate.py`:
  - `verify(summary: GroundedSummary, critical_set: CriticalSet) -> VerifiedSummary` where `VerifiedSummary = { summary: GroundedSummary (grounded claims only), dropped: list[Claim], flags: list[RuleFlag] }`.
  - Grounding: build the set of valid `(resource_type, id)` from `critical_set`; keep a claim iff **all** its `sources` are in that set; move the rest to `dropped`. Record a Langfuse "verification pass/fail" event (M0-6 helper) with counts.
- `verification/rules.py`:
  - `RuleFlag = { rule: str, severity: str, message: str, sources: list[SourceRef] }`.
  - `check_allergy_contraindications(critical_set) -> list[RuleFlag]` — normalize med name / allergen substance (case-insensitive substring + a small synonym set is fine for M1) and flag matches. Deterministic; runs regardless of the model output.
- Define `VerifiedSummary` / `RuleFlag` in `schemas/output.py`? **No** — to keep `schemas/` owned by M1-1, put these two small models in `verification/gate.py` / `rules.py`.

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_verification.py -q
```
Tests: a claim citing a source **not** in the critical set is dropped; a fully-grounded claim survives; a planted med-vs-allergy pair produces a `RuleFlag` **even when the summary omits it**; no false positive when med and allergen are unrelated.

## Definition of done
Ungrounded claims never pass; an allergy contraindication in the data is flagged deterministically independent of the model; results are recorded as verification events for the M3 dashboard.
