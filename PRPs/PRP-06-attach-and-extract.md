# PRP-06 — attach_and_extract + ingestion API

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-04, PRP-05 · **Blocks:** PRP-09, PRP-13 · **Needs key:** BUILD no (stubbed); **LIVE smoke YES**

## Goal (atomic)
The FR-1 tool + HTTP surface: accept a file, associate it with a patient, store
the source, extract structured facts, persist them, and return the strict JSON +
citations + FHIR refs — as one traced, correlation-ID-carrying operation.

## Context / files owned
- `agent/src/copilot/documents/ingest.py`, `agent/src/copilot/api/documents.py`,
  and the **only** Wave-3 edit to `main.py` (mount the ingestion router).
- Composes PRP-04 (`extract_*`) + PRP-05 (`store_source`, `persist_observations`).

## Contract
- `attach_and_extract(patient_id, file_path, doc_type) -> IngestResult` where
  `IngestResult = {source: SourceRef, extracted: LabReport | IntakeFacts,
  fhir_refs: list[SourceRef], confidence: float}`.
- `POST /patients/{patient_id}/documents` (multipart upload, `doc_type` field) →
  runs the tool, streams/returns `IngestResult`. Rejects unknown `doc_type` (422).
- Order: store source → extract → persist facts → return. correlation_id
  threaded through every step and into the FHIR writes.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_ingest.py tests/test_documents_api.py -q && pytest -q
```
- Integration (stubbed VLM + mocked OpenEMR): end-to-end returns a validated
  `LabReport` with per-value citations and fhir_refs; bad `doc_type` → 422.
- **LIVE acceptance smoke (needs key + Railway OpenEMR):** ingest a fixture lab
  PDF for real → extracted values match the fixture manifest, source doc + at
  least one Observation land in OpenEMR, and a **second** ingest of the same file
  does not duplicate (round-trip integrity).
- ruff clean; full suite green; no PHI in logs.

## Builder prompt (backend-dev → qa)
> Implement `documents/ingest.py::attach_and_extract(patient_id, file_path,
> doc_type)` composing PRP-04 extraction and PRP-05 persistence: store source →
> extract → persist → return `IngestResult` (source ref, validated extracted
> model, fhir refs, confidence). Add `api/documents.py` with `POST
> /patients/{id}/documents` (multipart; rejects unknown doc_type with 422) and
> mount it in `main.py`. Thread correlation_id through every step. Build + test
> with stubbed VLM and mocked OpenEMR. Then run the LIVE smoke against Railway
> OpenEMR with a fixture PDF and confirm values match the manifest, records land,
> and a re-ingest doesn't duplicate. ruff + full pytest green. Hand to qa.
