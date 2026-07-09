# PRP M2-1 · Full deterministic rule engine (dosage + interactions + allergy)

**Milestone:** M2 · **Depends on:** M1-6 (rules/gate), M1-1 (schemas) · **Blocks:** M2-5 · **Needs API key:** no

## Goal
Extend the M1 verification engine from one rule (allergy contraindication) to the full deterministic set (FR-9, UC-4): **drug-drug interactions** and **dosage thresholds**, plus the existing allergy check. Pure Python, model-independent; every flag fires from the retrieved data whether or not the model mentioned it.

## Context
- M1 built `verification/rules.py` (`RuleFlag{rule,severity,message,sources}`, `check_allergy_contraindications(critical_set)`) and `verification/gate.py` (`verify(summary, critical_set)` currently calls only the allergy check). Extend both.
- Inputs are the `CriticalSet` (M1-1): `medications`, `allergies`, `labs`, `problems`, `deltas`.

## Spec — own these files
- `verification/knowledge.py` (new): a small, **documented, source-cited** knowledge base — a table of well-known interacting pairs (e.g. warfarin+NSAID, warfarin+aspirin, ACE-inhibitor+potassium-sparing, statin+macrolide), and per-drug dosage ceilings for a handful of common meds (e.g. acetaminophen 4000 mg/day). Include a normalization/synonym helper (case-insensitive, drug-class synonyms). Keep it clearly a demo-scale KB with a docstring noting real deployments would use a licensed interaction database.
- `verification/rules.py` (extend): add `check_drug_interactions(critical_set) -> list[RuleFlag]` (cross-product of active meds against the interaction table; each flag cites both med `SourceRef`s), `check_dosage_thresholds(critical_set) -> list[RuleFlag]` (parse `Medication.dosage`, compare to the ceiling; flag over-threshold; skip unparseable rather than false-positive), and `run_all_rules(critical_set) -> list[RuleFlag]` unioning allergy + interaction + dosage. Deterministic; severity ordered (`high`/`medium`/`low`).
- `verification/gate.py` (extend): `verify(...)` calls `run_all_rules` instead of only the allergy check.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_rules_engine.py tests/test_verification.py -q
```
Tests: a planted interacting pair (e.g. warfarin + aspirin both active) yields a `RuleFlag` **even when the summary omits it**; a med dosed over its ceiling flags; a med under ceiling / unparseable dosage does **not** false-positive; unrelated meds yield no interaction flag; the existing allergy check + grounding tests still pass.

## Definition of done
The verification gate surfaces allergy, interaction, and dosage violations deterministically from the retrieved data, independent of the model — the M2 "planted interaction flagged even if the model omits it" acceptance is met at the engine level.
