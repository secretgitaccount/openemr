# PRP-15 — Ingest documents from the chart (read what OTHERS uploaded)

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-04, PRP-06 · **Blocks:** PRP-16 · **Needs key:** BUILD no (stubbed); LIVE-LOCAL smoke YES

## Why
In a real clinic the physician does NOT upload PDFs — the **front desk / nurse /
patient portal** scans documents into OpenEMR's Documents module, and the doctor
wants them *read*. The co-pilot must ingest documents **already in OpenEMR**, not
ones the doctor uploads. (Matches the assignment scenario: "a scanned lab PDF and
an intake form **uploaded by the front desk**.")

## Context / files owned
- NEW `agent/src/copilot/documents/chart_read.py`, NEW `agent/src/copilot/api/chart_documents.py`,
  a mount line in `main.py`, tests. Reuse PRP-04 `extract_*` + PRP-06 `persist`/
  `IngestResult`; import the page-render helper from `api/preview.py` (don't edit it).
- Read paths (per the OpenEMR investigation): list = `GET {fhir}/DocumentReference?patient=<uuid>`
  → each `content[].attachment.url` = `.../Binary/<id>`; bytes = `GET {fhir}/Binary/<id>`
  (returns decrypted raw bytes). Scopes: `user/DocumentReference.read` + `user/Binary.read`.

## Local setup this PRP must handle
- Ensure the READ OAuth client has `user/DocumentReference.read` + `user/Binary.read`
  (re-register+enable or update its scope column on local OpenEMR, like PRP-05 did
  for the write client). Confirm empirically.

## Contract
- `list_chart_documents(patient_id) -> list[ChartDocument]` where `ChartDocument =
  {doc_id, title, category, date, mimetype}` (from FHIR DocumentReference).
- `ingest_chart_document(patient_id, doc_id, doc_type) -> IngestResult` — fetch
  bytes via Binary → temp file → PRP-04 extract → PRP-06 persist → return. **Set the
  extraction `SourceCitation.source_id` = the OpenEMR document id** (stable) so the
  UI can map citation → chart document (fixes the temp-basename linkage from PRP-13).
- Endpoints (mount in main.py):
  - `GET /patients/{patient_id}/chart-documents` → list.
  - `POST /patients/{patient_id}/chart-documents/{doc_id}/ingest?doc_type=lab_pdf|intake_form` → IngestResult.
  - `GET /patients/{patient_id}/chart-documents/{doc_id}/page/{n}` → rendered page PNG (data URI) + dims, fetching the doc's bytes from OpenEMR (for the overlay).

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_chart_read.py tests/test_chart_documents_api.py -q && pytest -q
```
- Unit (OpenEMR FHIR mocked + stubbed VLM): list returns ChartDocuments; ingest
  fetches Binary bytes → validated LabReport whose citations carry
  `source_id == doc_id`; page endpoint returns PNG + dims; unknown doc_type → 422.
- **LIVE-LOCAL smoke:** upload a fixture PDF into local OpenEMR **as a
  non-physician** (Documents REST upload / a Front-Office-style path — the doctor
  does NOT upload it), then via the read client: list it, ingest+extract it, and
  confirm the extracted values match the fixture manifest. Clean up.
- ruff clean; full suite green.

## Builder prompt
See the launch message. Reuse extract+persist; only NEW files + main.py mount.
