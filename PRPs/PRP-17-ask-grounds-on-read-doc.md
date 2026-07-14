# PRP-17 — /ask grounds against the document the doctor is reading

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-15, PRP-09, PRP-10 · **Blocks:** PRP-18 · **Needs key:** BUILD no (stubbed); LIVE-LOCAL smoke YES

## Why
After the doctor reads a chart document (extracted values shown), asking "is this
glucose concerning per guidelines?" must ground against **that document's values**
(record facts) AND guideline evidence — today `/ask` with no attachment returns
`record_facts: 0`. Make the graph able to extract a **chart document by id**, and
reuse the read's extraction so the ask stays fast (no double VLM).

## Context / files owned
- EDIT `agent/src/copilot/graph/state.py` (Attachment), `agent/src/copilot/graph/workers.py`
  (intake_extractor), and NEW `agent/src/copilot/documents/extract_cache.py`
  (in-memory extraction cache, mirrors the Week-1 summary_cache HIPAA-by-inheritance
  design). Reuse PRP-15 `ingest_chart_document` / `chart_read`. Tests.
- Do NOT change the /ask endpoint contract shape beyond what Attachment allows.

## Contract
- `Attachment` gains `document_id: str | None = None`; `file_path: str | None = None`
  (one of the two required). Frozen/extra=forbid preserved.
- `intake_extractor`: if `document_id` set → extract that chart document (reuse the
  PRP-15 chart-read extraction path); else the existing `file_path` path.
- **Extraction cache** (`extract_cache.py`): in-memory, keyed by
  `(patient_id, document_id, content_hash)`; `ingest_chart_document`/the chart-read
  extraction **populates** it, and the graph's chart-doc extraction **reads** it —
  so the doctor's "Read" warms it and the subsequent "Ask" reuses it (no second VLM
  call). Bounded LRU, no TTL, no PHI in keys/logs (same rationale as summary_cache).
- Result: `/ask` with `attachments:[{document_id, doc_type}]` returns a W2Answer
  whose `record_facts` include that document's values (grounded, cited to the doc id)
  AND `guideline_evidence`.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_extract_cache.py tests/test_graph.py tests/test_w2flow.py -q && pytest -q
```
- Unit (stubbed VLM/RAG, no key): a document_id attachment routes through
  intake_extractor → record_facts populated + cited to the doc id; a cache hit
  avoids a second extraction call (assert the extractor is invoked once across a
  warm + ask); file_path attachments still work; Attachment with neither → 422/validation error.
- **LIVE-LOCAL smoke:** for a front-desk-seeded doc on patient a2372c03, POST /ask
  with `attachments:[{document_id:<doc>, doc_type:"lab_pdf"}]` and question "Is this
  glucose result concerning per guidelines?" → answer references Glucose 168 as a
  record fact (cited to the doc) + guideline evidence; record_facts > 0.
- Full suite green; ruff clean; only the owned files touched.

## Builder prompt
See the launch message.
