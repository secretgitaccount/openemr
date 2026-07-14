# PRP-16 — Doctor UI reads chart documents (no doctor upload)

**Role:** frontend-dev · **QA:** qa · **Depends on:** PRP-15 · **Needs key:** no (drives the running agent)

## Why
The physician does not upload PDFs. Replace the upload control with a picker of
documents **already on the chart** (uploaded by front desk / nurse / portal). The
doctor selects one and the co-pilot reads it — same extraction view, click-to-
source overlay, and grounded answer as before.

## Context / files owned
- `agent/src/copilot/ui/index.html` (+ `ui/**`). Consumes PRP-15 endpoints:
  `GET /patients/{id}/chart-documents` (list), `POST .../chart-documents/{doc_id}/ingest`
  (read+extract), `GET .../chart-documents/{doc_id}/page/{n}` (overlay image).

## Behavior
- **Remove the file-upload affordance** (drag/drop / file picker) from the doctor UI.
- On patient select, show **"Documents on this chart"** — the list from
  `GET /patients/{id}/chart-documents` (title · category · date). If empty, say so.
- Doctor clicks a document → pick doc_type (or infer) → `POST .../ingest` → render
  the extracted values + confidence (as today).
- Click-to-source: resolve the page image from `GET .../chart-documents/{doc_id}/page/{n}`
  (the citation `source_id` now equals the OpenEMR doc id — use it directly, no
  in-memory file), overlay the bbox. Guideline citations keep the snippet path.
- `/ask` unchanged (record facts vs guideline evidence).
- CSP-clean, no client-side PHI.

## Validation
- Behavioral (agent running + a front-desk-uploaded doc in local OpenEMR): the
  picker LISTS that document; selecting it shows extracted values; clicking a value
  highlights the correct word-box on the fetched page; **no upload control is
  present**. Screenshot the real flow.
- `test_ui.py` + full suite green; no external hosts; no client-side PHI.

## Builder prompt
See the launch message.
