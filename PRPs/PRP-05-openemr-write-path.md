# PRP-05 — OpenEMR write path

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-00, PRP-02 · **Blocks:** PRP-06 · **Needs key:** no (OpenEMR mocked in tests)

## Goal (atomic)
Persist to OpenEMR **idempotently**: store a source document and write derived
FHIR Observations, per the write path the **PRP-00 spike** selected. Re-running
the same ingestion must not create duplicate or untraceable records (FR-10).

## Context / files owned
- `agent/src/copilot/documents/openemr_write.py`, `agent/tests/test_openemr_write.py`.
- Reuse Week-1 `openemr/client.py` (httpx) + `openemr/oauth.py`. Read
  `PRPs/_spikes/PRP-00-result.md` for the chosen endpoints/scopes/dedup key.
- Consumes PRP-02 `LabReport` / `SourceCitation`; returns Week-1 `SourceRef`.

## Contract
- `store_source(patient_id, file_path, doc_type) -> SourceRef` — uploads the
  source doc; tags it with a content-hash identifier for dedup.
- `persist_observations(patient_id, report: LabReport) -> list[SourceRef]` —
  writes each `LabObservation` as an Observation, tagged with an identifier
  derived from `{patient, test, collection_date}` so re-writes are upserts.
- On failure: typed `AgentError` (retriable flagged), `ToolResult.partial=True`
  with `missing` naming what didn't persist — never a silent drop.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_openemr_write.py -q && pytest -q
```
- OpenEMR mocked (`respx`/`pytest-httpx`): happy path returns real-shaped ids.
- **Idempotency:** two identical writes → one logical record (dedup by tag),
  asserted (the graded round-trip-integrity check).
- Write error → typed `AgentError`, `partial=True`, correlation_id logged, no PHI.
- ruff clean; full suite green.

## Builder prompt (backend-dev → qa)
> Read `PRPs/_spikes/PRP-00-result.md` for the selected OpenEMR write path,
> endpoints, scopes, and dedup key. Implement `documents/openemr_write.py` with
> `store_source(...)` and `persist_observations(...)` reusing the Week-1 httpx
> client + OAuth. Make writes **idempotent** via a content/identity tag so a
> re-run upserts instead of duplicating (FR-10). Failures return a typed
> AgentError with `partial`/`missing`, correlation_id bound, no PHI in logs.
> Test with OpenEMR mocked (respx): happy path, idempotent re-write (one logical
> record), and error path. ruff + full pytest green. Hand to qa.
