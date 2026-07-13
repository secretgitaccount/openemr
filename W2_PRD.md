# W2_PRD — Clinical Co-Pilot, Week 2 (MVP build)

> Product requirements for the Week 2 build. **What & why & done-criteria.**
> The **how** (module layout, tech decisions, diagram) lives in
> `./W2_ARCHITECTURE.md`. The **build units** live in `./PRPs/`.
> Scope is **MVP-tight** per the assignment ("narrower is stronger").

## 1. Problem & user

A physician prepping for a follow-up visit has structured OpenEMR data, but the
important recent information is buried in a **scanned lab PDF** and a **patient
intake form** uploaded by the front desk. They ask: _what changed, what should I
pay attention to, and what evidence supports the recommendation?_

The Week 2 Co-Pilot must ingest those documents, extract structured facts **with
citations**, retrieve **guideline evidence**, and return a **grounded** answer —
useful even when the scan is imperfect, the record is incomplete, or the user
asks a follow-up. It must separate **patient-record facts** from **guideline
evidence**, and every clinical claim must point back to a source.

## 2. Goals (MVP)

| # | Goal | Success criterion |
|---|---|---|
| G1 | Ingest 2 document types | `lab_pdf` + `intake_form` upload → strict-schema JSON, source stored in OpenEMR, every fact linked to `{doc,page,field}` |
| G2 | Hybrid RAG + rerank | small guideline corpus, BM25+dense retrieval, local cross-encoder rerank, top-k evidence with source metadata |
| G3 | Supervisor + 2 workers | LangGraph graph routing to `intake-extractor` + `evidence-retriever`, **handoffs logged & inspectable** |
| G4 | Eval-driven CI gate | 50-case golden set, boolean rubrics, **PR-blocking git hook** that fails on >5% regression / below threshold |
| G5 | Integrate & demo | deployed app, source-grounded UI (click-to-source + PDF bbox overlay), latency/cost report, 3–5 min video |

## 3. Non-goals (explicitly deferred past MVP)

Critic agent, 3rd document type, lab-trend chart, OpenAPI/Postman/data-model/
backup docs. These are required for **Final**, scheduled for the next pass — not
dropped. (The **citation contract** is MVP; the **critic** is deferred.)

## 4. Functional requirements

- **FR-1 `attach_and_extract(patient_id, file_path, doc_type)`** — supports
  `lab_pdf`, `intake_form`; stores source in OpenEMR; returns strict-schema
  JSON; persists derived facts as **OpenEMR records via REST** (FHIR-native create
  is unavailable on this build — PRP-00 spike; the spec allows "FHIR resources
  **or OpenEMR records**").
- **FR-2 Strict schemas** — Pydantic, `frozen`, `extra="forbid"`. Lab fields ≥
  {test name, value, unit, reference range, collection date, abnormal flag,
  source citation}. Intake fields ≥ {demographics, chief concern, current
  medications, allergies, family history, source citation}.
- **FR-3 Schema is source of truth** — raw VLM output never bypasses validation;
  a field the model can't ground is dropped/flagged, not surfaced.
- **FR-4 Hybrid RAG** — sparse (BM25) + dense (FAISS) retrieval → local
  cross-encoder rerank → only top grounded evidence to the answer model.
- **FR-5 Graph** — supervisor decides extract? / retrieve? / answer-ready?;
  explicit logged handoffs; each worker span a child of the supervisor span;
  correlation ID propagates from Week 1 middleware into every node/call/write.
- **FR-6 Citation contract** — every clinical claim carries machine-readable
  `{source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value}`.
  Patient-record facts vs. guideline evidence are separated **by type**.
- **FR-7 PDF bbox overlay** — click-to-source highlights the exact region a
  lab value was extracted from (text-layer PDF → `pdfplumber` word boxes).
- **FR-8 Eval gate** — 50 boolean-rubric cases across `schema_valid`,
  `citation_present`, `factually_consistent`, `safe_refusal`, `no_phi_in_logs`;
  PR-blocking hook fails on >5% category regression / below pass threshold.
- **FR-9 Observability** — per encounter: tool sequence, latency by step, token
  usage, cost estimate, retrieval hits, extraction confidence, eval outcome.
  **No raw PHI in logs.**
- **FR-10 Round-trip integrity** — uploaded docs + derived observations must not
  create duplicate or untraceable OpenEMR records.

## 5. Non-functional requirements

- **NFR-1 HIPAA-minded** — synthetic/demo data only; `scrub_phi` on everything
  that leaves the process (logs, traces, eval datasets, cost reports). RAG stack
  is fully local — no third-party data processor, no new BAA.
- **NFR-2 Timeouts/retries** — every outbound LLM/VLM/retrieval call wrapped
  (tenacity), with a documented timeout.
- **NFR-3 Typed contracts** — every interface (ingestion, RAG, handoffs, FHIR
  writes) has a Pydantic contract; a Week-1 schema change carries a migration note.
- **NFR-4 `/ready`** — validates Week 2 deps (doc storage, vector index,
  reranker); returns *degraded*, not binary, when one is down.
- **NFR-5 Reproducible eval set** — golden set lives in the repo, not only a DB.
- **NFR-6 Week 1 unchanged** — Week 1 behavior compounds; only additive changes
  (FHIR write path + new Langfuse spans).

## 6. Acceptance (the graded hard gate)

Graders will **inject a small regression and confirm the CI gate fails.** If the
eval gate does not block it, the build does not pass. This makes **PRP-11 +
PRP-12** (golden set + PR-blocking hook) the highest-priority deliverables after
the ingestion/RAG/graph core exists to evaluate.

## 7. Milestone map → PRPs

| Stage | PRPs |
|---|---|
| P0 spike + scaffold | PRP-00, PRP-01 |
| Stage 1 ingestion | PRP-02, PRP-03, PRP-04, PRP-05, PRP-06 |
| Stage 2 RAG | PRP-07, PRP-08 |
| Stage 3 graph | PRP-09, PRP-10 |
| Stage 4 eval gate | PRP-11, PRP-12 |
| Stage 5 integrate | PRP-13, PRP-14 |

See `./PRPs/README.md` for the dependency DAG and execution order.
