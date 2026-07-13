# PRP-02 — Document schemas + validation tests

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-01 · **Blocks:** PRP-04, PRP-05 · **Needs key:** no

## Goal (atomic)
Define the **canonical extraction contracts** — the source of truth that raw VLM
output must pass through. Strict, frozen, `extra="forbid"`. Plus the citation
contract shared across the whole answer path.

## Context / files
- Create `agent/src/copilot/documents/schemas.py`.
- Extend the Week-1 `SourceRef` pattern (`copilot/schemas/core.py`) — do not
  duplicate it; compose with it.
- Tests: `agent/tests/test_document_schemas.py`.

## Contract
- `SourceCitation` — `{source_type, source_id, page_or_section, field_or_chunk_id,
  quote_or_value}` (the machine-readable citation contract, FR-6).
  `source_type ∈ {lab_pdf, intake_form, guideline, fhir}`.
- `LabObservation` — test_name, value, unit, reference_range, collection_date,
  abnormal_flag (enum: normal/high/low/critical/unknown), citation: SourceCitation.
- `LabReport` — patient_ref, report_date, observations: list[LabObservation],
  extraction_confidence: float [0,1], source: SourceCitation.
- `IntakeFacts` — demographics (name/dob/sex/…), chief_concern, current_medications:
  list[str], allergies: list[str], family_history: list[str], each grounded by a
  citation; extraction_confidence; source: SourceCitation.
- All fields that can be "not found" are `None`-able and flagged, never invented.

## Validation gates
- [ ] Every model `frozen`, `extra="forbid"`; unknown key → ValidationError (test).
- [ ] Missing required grounding (citation) → ValidationError (test).
- [ ] `abnormal_flag` rejects out-of-enum values (test).
- [ ] Round-trip `model_dump_json()`/`model_validate_json()` stable (test).
- [ ] ruff clean; docstrings mirror `schemas/core.py` style.

## Agent launch prompt
> Create `agent/src/copilot/documents/schemas.py` with strict Pydantic v2 models
> (`frozen`, `extra="forbid"`) for the Week 2 extraction contracts: `SourceCitation`
> (source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value),
> `LabObservation`, `LabReport`, `IntakeFacts`. Compose with the existing
> `copilot.schemas.core.SourceRef`; match its docstring/config style. Required lab
> fields: test name, value, unit, reference range, collection date, abnormal flag
> (enum), source citation. Required intake fields: demographics, chief concern,
> current medications, allergies, family history, source citation. Fields that can
> be absent are None-able and flagged, never invented. Write
> `agent/tests/test_document_schemas.py` proving: unknown-key rejection, missing-
> citation rejection, enum enforcement, JSON round-trip. ruff + pytest green.
