# PRP-05 — OpenEMR write path (REST)

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-00, PRP-02 · **Blocks:** PRP-06 · **Needs key:** no Anthropic key; LIVE smoke uses LOCAL OpenEMR write creds

## Goal (atomic)
Persist to the **local** OpenEMR **idempotently** via REST: store a source
document and persist derived facts as OpenEMR records. **FHIR-native create is
unavailable on this build** (PRP-00 spike) — do not attempt FHIR writes. The spec
accepts "FHIR resources **or OpenEMR records**"; REST records satisfy it, and a
re-run must not duplicate (FR-10).

## Context / files owned
- `agent/src/copilot/documents/openemr_write.py`, `agent/tests/test_openemr_write.py`.
- **Read `PRPs/_spikes/PRP-00-result.md` first** — it has the exact endpoints,
  scopes, field-name gotcha, and dedup key.
- Reuse Week-1 `openemr/client.py` (httpx) + `openemr/oauth.py`. Consumes PRP-02
  `LabReport` / `SourceCitation`; returns Week-1 `SourceRef`.

## Local setup this PRP must handle (one-time, dev)
- Enable the **Standard REST API (`api:oemr`)** on local OpenEMR; ensure the
  password-grant `admin` user has patients/docs + encounter write ACLs.
- **Re-register a write-scoped OAuth client** (`openid offline_access api:oemr
  api:fhir` + `user/document.crs user/encounter.crus user/vital.crus` + the
  existing read scopes) and **enable it** (`is_enabled=1`). Persist creds to the
  agent env (gitignored). Document the steps in the module/PR.

## Contract (per spike, base `http://localhost:8300/apis/default`)
- `store_source(patient_id, file_path, doc_type) -> SourceRef` —
  `POST /api/patient/:pid/document`, multipart field **`document`**, `path` (+
  optional `eid`) as **query params**. Filename embeds the source **SHA-256**;
  pre-check `GET /api/patient/:pid/document?path=<folder>` and skip if the hash
  already exists (dedup). Recover the id from the listing (`insertAtPath` returns
  only `true`).
- `persist_observations(patient_id, report: LabReport) -> list[SourceRef]` —
  create/reuse **one ingestion encounter per source doc**
  (`POST /api/patient/:puuid/encounter`), then persist each derived value bound
  to that encounter (vitals via `.../encounter/:eid/vital`, or the appropriate
  REST record type). Dedup by `(pid, eid, key)` → re-run upserts.
- Failure → typed `AgentError` (retriable flagged), `ToolResult.partial=True`
  with `missing`; never a silent drop; correlation_id bound; no PHI in logs.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_openemr_write.py -q && pytest -q
```
- **Unit (OpenEMR mocked, respx/pytest-httpx):** happy path returns real-shaped
  ids from the listing; the multipart uses field `document` + query `path`
  (asserted); idempotent re-write (same SHA-256 already present → one logical
  record, no duplicate); error path → typed `AgentError`, `partial=True`.
- **LIVE-LOCAL smoke (local docker, write-scoped client):** upload a fixture PDF
  → it appears in the document listing with a matching hash; re-upload the same
  file → no duplicate; one derived vital lands under the ingestion encounter.
  Clean up the synthetic records afterward.
- ruff clean; full suite green.

## Builder prompt (backend-dev → qa)
> Read `PRPs/_spikes/PRP-00-result.md`. Implement `documents/openemr_write.py`
> with `store_source(...)` (REST `POST /api/patient/:pid/document`, multipart
> field `document`, `path` query param, SHA-256 filename, dedup via the document
> listing) and `persist_observations(...)` (one ingestion encounter per source,
> derived values bound to it, dedup by (pid,eid,key)). Reuse the Week-1 httpx
> client + OAuth; do the local setup (enable `api:oemr`, register+enable a
> write-scoped client, save creds to the gitignored env) and document it.
> Failures return typed AgentError with partial/missing, correlation_id bound, no
> PHI. Test with OpenEMR mocked (happy, idempotent re-write, error) AND run a
> live-local smoke against localhost:8300 proving no-duplicate on re-upload; clean
> up after. ruff + full pytest green. Hand to qa.
