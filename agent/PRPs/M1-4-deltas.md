# PRP M1-4 · Delta computation since last visit

**Milestone:** M1 · **Depends on:** M1-1, M1-3 · **Blocks:** M1-7 · **Needs API key:** no

## Goal
Compute what changed since the last visit across encounters, meds, problems, and labs (FR-5, UC-1). A diff against a moving reference point — this is reasoning the static chart can't show.

## Context
- Reuse `FhirClient`, the M1-3 retrieval helpers where useful, `trace(...)`, M1-1 `Deltas`/`Encounter`.
- Own new file only: `openemr/deltas.py`. **Do not edit `openemr/tools.py` or `openemr/retrieval.py`** (import from them).
- FHIR: `Encounter?patient=&_sort=-date` to find the reference visit; then window meds/problems/labs by `authoredOn`/`onset`/`recordedDate`/`effectiveDateTime` ≥ the visit before last.

## Spec — `openemr/deltas.py`
- `get_encounters_since(patient_id, since, *, client) -> ToolResult[list[Encounter]]`
- `get_deltas_since_last_visit(patient_id, *, client) -> ToolResult[Deltas]` — determine the reference point (the second-most-recent encounter, or `None` if <2 encounters → return an empty `Deltas` with `reference_visit=None`, **not** an error), then compute `new_meds`, `stopped_meds`, `new_problems`, `new_labs`, `new_encounters` since it. Each item keeps its `SourceRef`. Wrap in `trace("get_deltas_since_last_visit")`.
- Graceful: if the encounter history can't be fetched, return `Deltas(reference_visit=None)` and surface it via `ToolResult.partial=True, missing=["deltas"]` (FR-11) rather than raising.

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_deltas.py -q     # unit: reference-point selection + windowing (mocked FHIR)
# LIVE:
python -m copilot.openemr.deltas --patient <seeded_id>   # prints reference visit + counts of changes
```
Tests: patient with ≥2 encounters yields a non-null reference and windowed changes; patient with <2 encounters yields an empty `Deltas` (not an error); a fetch failure yields a partial result flagged in `missing`.

## Definition of done
Deltas compute against live seeded data with a sensible reference point, carry sources on every changed item, and degrade to an empty-but-valid result when history is thin or unavailable.
