# Migrations — Week 1 → Week 2

**Summary: Week 2 introduced no database schema migration.** This note documents
that decision explicitly, as the engineering requirements ask ("any schema change
from Week 1 must be accompanied by a migration note").

## Data-store changes: none

Week 2 writes derived facts into **existing OpenEMR tables** through the REST API
— it does not create or alter any table:

- **Source documents** are stored via `POST /api/patient/:pid/document` (the
  existing OpenEMR documents store).
- **Derived lab observations** are written as **encounter / vital records** via
  the existing REST endpoints (`documents/openemr_write.py`). FHIR-native
  resource creation is unavailable on this OpenEMR build (confirmed by the PRP-00
  spike), so the spec's "FHIR resources **or** OpenEMR records" allowance is
  satisfied with OpenEMR records.

Because nothing in the schema changed, there is no forward/backward migration to
run and no rollback script to maintain.

## Contract (schema-as-code) changes: additive only

The typed contracts introduced in Week 2 are all **new, additive** Pydantic
models — no Week-1 contract was modified in a breaking way:

| New contract | File | Purpose |
|---|---|---|
| `LabReport` / `LabObservation` | `documents/schemas.py` | strict lab-PDF extraction schema |
| `IntakeFacts` / `IntakeDemographics` | `documents/schemas.py` | strict intake-form extraction schema |
| `SourceCitation` | `documents/schemas.py` | the 5-field citation contract |
| `IngestResult` | `documents/ingest.py` | ingestion tool return type |
| `GraphState` / `Handoff` / `WorkerTiming` | `graph/state.py` | LangGraph channel state + routing log |
| `GraphInput` / `GraphResult` | `graph/state.py` | graph run I/O boundary |
| `W2Answer` / `AnswerDiagnostics` | `graph/answer.py` | final grounded answer + metrics sink |
| `EncounterMetrics` / `StepLatency` / `WorkerLatency` | `observability.py` | per-encounter observability |

All are `extra="forbid"` and (except the mutable metrics sinks) `frozen`, so an
unexpected field is a hard validation error, never a silent write. The Week-1
`GroundedSummary` / `Claim` / `SourceRef` / `CriticalSet` contracts are reused
**unchanged** (the verification gate is shared, not forked).

## If a real schema change is ever needed

Should a future change require an OpenEMR schema alteration, it must go through
OpenEMR's own Doctrine Migrations (see the root `CLAUDE.md` → "Database and
Global Settings"), with a paired migration note added here describing the change,
the backfill, and the rollback path.
