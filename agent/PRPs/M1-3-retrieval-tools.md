# PRP M1-3 · Tiered parallel critical-set retrieval

**Milestone:** M1 · **Depends on:** M1-1, M0 (FhirClient) · **Blocks:** M1-4, M1-6, M1-7 · **Needs API key:** no

## Goal
The critical-set retrieval tools (FR-3/4): fetch active meds, allergies, recent/abnormal labs, open problems **in parallel** as the user token, each record carrying `source_id`+`timestamp`. Per-tool failure degrades gracefully (FR-11) — a partial `CriticalSet` names what couldn't be retrieved; distinguish "no data on file" from "no known".

## Context
- Reuse `FhirClient.get(path, params=)`, `trace(...)`, the M1-1 schemas, and the defensive FHIR-mapping style already established in `openemr/tools.py` (guarded field access; never half-valid models).
- Own new file only: `openemr/retrieval.py`. **Do not edit `openemr/tools.py`.**
- FHIR endpoints (as the user, `user/*` scope): `MedicationRequest?patient=&status=active`, `AllergyIntolerance?patient=`, `Observation?patient=&category=laboratory` (+ `date=ge<since>`), `Condition?patient=&clinical-status=active`.

## Spec — `openemr/retrieval.py`
- `get_active_medications(patient_id, *, client) -> ToolResult[list[Medication]]`
- `get_allergies(patient_id, *, client) -> ToolResult[list[Allergy]]` — empty result ⇒ `partial=False`, `missing=[]`, data `[]` meaning **"no known allergies"**; a *failed* fetch ⇒ `partial=True`, `missing=["allergies"]` meaning **"not retrieved"**. This distinction is the FR-11 crux — encode it in the `ToolResult`.
- `get_recent_labs(patient_id, since=None, *, client) -> ToolResult[list[LabResult]]` — flag `abnormal` from the FHIR `interpretation`/reference-range when present.
- `get_problem_list(patient_id, *, client) -> ToolResult[list[Problem]]`
- `get_critical_set(patient_id, *, client) -> CriticalSet` — `asyncio.gather(..., return_exceptions=True)` across the four tools; assemble a `CriticalSet`, folding each tool's failure into `missing` rather than raising. Wrap in `trace("get_critical_set")`; span records counts + `missing`, never clinical values (NFR-4).

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_retrieval.py -q     # unit: FHIR->schema mapping + partial/missing semantics (mocked)
# LIVE (dev stack up, chart seeded):
python -m copilot.openemr.retrieval --patient <seeded_id>   # prints counts + any missing; one tool forced to fail still returns a partial set
```
Tests must cover: a populated patient maps all four record types with sources; an empty allergy list ⇒ "no known" (not missing); a raised fetch ⇒ recorded in `missing`, other tools still return; `get_critical_set` never raises on a single-tool failure.

## Definition of done
The four critical-set tools return typed, source-bound records against live seeded OpenEMR; parallel assembly degrades to a partial set on any single failure; "no data on file" and "no known" are distinguishable in the output.
