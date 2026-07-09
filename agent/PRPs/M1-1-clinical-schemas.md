# PRP M1-1 · Clinical + source-bound output schemas

**Milestone:** M1 · **Depends on:** M0-5 (`schemas/core.py`) · **Blocks:** M1-2..M1-7 · **Needs API key:** no

## Goal
Add the M1 domain contracts. Pydantic v2 is the source of truth (NFR-3); every retrieval tool and the LLM's structured output bind to these. Each clinical record carries a stable `source_id` + `timestamp` so claims can be grounded and the UI can link back (PRD §11).

## Context
- Reuse `SourceRef`, `ToolResult[T]`, `AgentError` from `schemas/core.py` (already built).
- Own the `schemas/` package: create new modules, wire them into `schemas/__init__.py`. **No other PRP edits `schemas/`.**

## Spec — new files only
- `schemas/clinical.py`:
  - `ScheduledPatient` — `{ patient_id, name, start, appointment_id, source: SourceRef }` (FR-1).
  - `PanelDecision` — `{ in_panel: bool, reason: str, break_glass: bool = False, source: SourceRef | None }` (FR-2).
  - `Medication` — `{ id, name, status, dosage: str | None, source: SourceRef }`.
  - `Allergy` — `{ id, substance, reaction: str | None, criticality: str | None, source: SourceRef }`.
  - `LabResult` — `{ id, name, value: str | None, unit: str | None, effective: datetime | None, abnormal: bool | None, source: SourceRef }`.
  - `Problem` — `{ id, name, clinical_status: str | None, onset: date | None, source: SourceRef }`.
  - `Encounter` — `{ id, kind: str | None, start: datetime | None, source: SourceRef }`.
  - `Deltas` — `{ reference_visit: datetime | None, new_meds, stopped_meds, new_problems, new_labs, new_encounters: list[...] }` (FR-5).
  - `CriticalSet` — the tiered bundle: `{ medications, allergies, labs, problems: list[...], deltas: Deltas | None, retrieved_at, missing: list[str] }` (FR-4/11).
- `schemas/output.py` (source-bound output, FR-8):
  - `Claim` — `{ text: str, sources: list[SourceRef] }`. A claim with an empty `sources` is structurally invalid *as a fact* (verification drops it in M1-6).
  - `GroundedSummary` — `{ headline: str, must_knows: list[Claim], whats_changed: list[Claim], caveats: list[str] }`. This is the schema the LLM's structured output binds to (M1-5).
- All models: pydantic v2, `frozen=True` for the value objects, explicit types (no bare `dict`/`Any` in public fields).

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_clinical_schemas.py -q
```
Tests: each model parses a valid sample and rejects an invalid one; `Claim(sources=[])` is constructible but flagged by a `is_grounded` helper/property; `CriticalSet` round-trips; every record type carries a `SourceRef`.

## Definition of done
The M1 contracts import cleanly, validate/reject sample data, and are the single source of truth for every M1 tool and the LLM's structured output.
