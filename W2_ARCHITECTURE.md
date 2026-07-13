# W2_ARCHITECTURE — Clinical Co-Pilot, Week 2

> **Status:** planning seed (architecture defense). Sections marked _TBD_ are
> filled in as each stage lands. Week 1 baseline lives in `./ARCHITECTURE.md`;
> this document covers **only** the Week 2 multimodal + multi-agent additions.

## 1. Week 1 baseline vs. Week 2 additions

Week 1 shipped a read-only agent over OpenEMR's OAuth2/SMART-FHIR API: grounded,
cited chart summaries with deterministic verification, panel + role gates, PHI
scrubbing, Langfuse observability, a starter eval suite, and a Railway
deploy. **Week 2 adds two capabilities without forking that core:** the agent
can now _see_ clinical documents, and it _routes_ work across a small,
inspectable multi-agent graph — gated by an eval-driven CI that blocks
regressions.

Everything in Week 1 is reused unchanged except **two** deliberate changes,
both additive:
- OpenEMR gains a **write path** (was read-only) — source documents + derived
  FHIR Observations round-trip back in.
- Langfuse gains **new trace spans** for the graph + ingestion flows.

## 2. Architecture (red = new Week 2 infra)

```mermaid
flowchart TB
    %% ===== RED = new Week 2 infra · BLUE = existing Week 1 =====

    subgraph CLIENT["Client"]
        UI["Browser UI<br/>chart summary · chat"]
        UP["Doc upload · PDF bbox overlay<br/>click-to-source"]:::new
    end

    subgraph AGENT["Agent Service — FastAPI on Railway"]
        MW["Correlation-ID middleware · structlog"]
        SUM["/summary · /chat · /patients"]
        VER["Verification gate<br/>rules + knowledge"]
        RDY["/ready EXTENDED<br/>doc store · vector index · reranker"]:::new
        ING["Ingestion API<br/>attach_and_extract(patient, file, doc_type)"]:::new
    end

    subgraph INGEST["Document Ingestion — NEW"]
        VLM["Claude Vision VLM<br/>extract → strict schema"]:::new
        PDF["pdfplumber<br/>word bounding boxes"]:::new
        DS["Strict schemas<br/>LabReport · IntakeForm · SourceCitation"]:::new
        FW["OpenEMR FHIR WRITES<br/>DocumentReference · Observation"]:::new
    end

    subgraph GRAPH["Multi-Agent Graph — LangGraph — NEW"]
        SUP["Supervisor<br/>routes + logged handoffs"]:::new
        WEX["intake-extractor worker"]:::new
        WEV["evidence-retriever worker"]:::new
        CRIT["Critic (deferred)<br/>rejects uncited / unsafe"]:::newd
    end

    subgraph RAG["Hybrid RAG — fully local — NEW"]
        BM25["BM25 keyword<br/>rank-bm25"]:::new
        FAISS["FAISS dense index"]:::new
        EMB["sentence-transformers<br/>embeddings + cross-encoder rerank"]:::new
        CORP["Guideline corpus<br/>ADA · ACC/AHA · USPSTF"]:::new
    end

    subgraph EXT["Existing external services"]
        OEMR["OpenEMR + MySQL<br/>Railway · OAuth2/SMART-FHIR"]
        CLAUDE["Claude API<br/>synthesis LLM"]
        LF["Langfuse<br/>traces + eval dataset"]
    end

    subgraph EVAL["Eval-driven CI — NEW"]
        GOLD["50-case golden set<br/>boolean rubrics"]:::new
        HOOK["PR-blocking git pre-push hook<br/>fail on >5% regression"]:::new
    end

    UI --> MW --> SUM --> OEMR
    SUM --> VER --> CLAUDE
    UP --> ING
    ING --> VLM --> DS
    ING --> PDF --> UP
    DS --> FW --> OEMR
    SUM --> SUP
    SUP --> WEX --> VLM
    SUP --> WEV
    SUP --> CRIT
    WEV --> BM25 --> EMB
    WEV --> FAISS --> EMB
    EMB --> CORP
    SUP --> CLAUDE
    RDY -.checks.-> FW
    RDY -.checks.-> FAISS
    RDY -.checks.-> EMB
    SUP -.traces.-> LF
    ING -.traces.-> LF
    GOLD --> HOOK -.runs.-> SUP

    classDef new fill:#ffdede,stroke:#c0281e,stroke-width:3px,color:#7a0a0a;
    classDef newd fill:#fff0f0,stroke:#c0281e,stroke-width:2px,stroke-dasharray:5 4,color:#7a0a0a;
    classDef existing fill:#e8eef7,stroke:#2d5a9e,color:#12243d;
    class UI,MW,SUM,VER,OEMR,CLAUDE,LF existing;

    style INGEST fill:#fff6f6,stroke:#c0281e
    style GRAPH fill:#fff6f6,stroke:#c0281e
    style RAG fill:#fff6f6,stroke:#c0281e
    style EVAL fill:#fff6f6,stroke:#c0281e
    style CLIENT fill:#f4f7fb,stroke:#2d5a9e
    style AGENT fill:#f4f7fb,stroke:#2d5a9e
    style EXT fill:#f4f7fb,stroke:#2d5a9e
```

**Reading it:** four new red subsystems (Ingestion, Graph, RAG, Eval CI) bolt
onto an otherwise-blue Week 1 core. The only two red edges into blue boxes are
the new FHIR write path and the new Langfuse spans — the sole places Week 2
touches Week 1 behavior.

## 3. Technology decisions

### 3.1 New in Week 2

| Capability | Tech | Choice | Rationale |
|---|---|---|---|
| Orchestration | **LangGraph** | supervisor + 2 workers, in-memory state | inspectable graph, explicit logged handoffs |
| Dense vectors | **FAISS** (`faiss-cpu`) | in-process flat index | no server; rebuilds <1s for a tiny corpus |
| Keyword retrieval | **rank-bm25** | BM25Okapi | sparse half of hybrid; pure-Python |
| Embeddings + rerank | **sentence-transformers** | `bge-small-en-v1.5` embed + `ms-marco-MiniLM-L-6-v2` cross-encoder | local; "equivalent reranker" allowed; small weights |
| Vision extraction | **Claude vision** (existing SDK, new usage) | Opus 4.8, PDF/image blocks → strict `parse()` | all-Anthropic; schema constrains VLM output |
| PDF text + bboxes | **pdfplumber** | word-level bounding boxes | powers citation overlay w/o OCR infra |
| Generate demo PDFs | **reportlab** | dev/test asset only | synthetic text-layer PDFs, no real PHI |
| Git gate | **native git pre-push hook** | shell script | runs 50-case suite; fails on >5% regression |

Heaviest transitive dep: **PyTorch** (via sentence-transformers). Mitigated by
choosing small model weights (~80–130 MB each), not the large `bge-reranker`
variants — keeps the Railway image lean.

### 3.2 Reused from Week 1 (no new tech)

Pydantic v2 (strict contracts) · Langfuse (traces + eval dataset) · pytest
(golden-set runner) · structlog (structured logs + correlation IDs) · tenacity
(retries/timeouts/breakers) · `scrub_phi` (PHI scrubbing + `no_phi_in_logs`
check) · FastAPI auto-generated OpenAPI 3.0 · vanilla HTML/CSS UI · Railway +
Docker.

### 3.3 Open decisions

- **FAISS vs sqlite-vec** — leaning FAISS (simplicity); sqlite-vec if we want a
  single backup-able index file for the backup/recovery eng-req. _TBD_
- **Vision model** — Opus 4.8 (best extraction) vs Sonnet (cheaper/faster).
  Cost report will decide. _TBD_

## 4. Document ingestion flow — _TBD (Stage 1)_

`attach_and_extract(patient_id, file_path, doc_type)` for `lab_pdf` and
`intake_form`: store source in OpenEMR → Claude vision → strict schema (schema
is the source of truth; raw VLM output never bypasses validation) → persist
derived facts as FHIR Observations → link every fact to `{doc, page, field}`.
**Bounding-box strategy:** generate text-layer demo PDFs so `pdfplumber` yields
exact word boxes; one document degraded to image-only to demo graceful
extraction. FHIR write path pending the P0 spike.

## 5. Worker graph — _TBD (Stage 3)_

One supervisor routing to `intake-extractor` and `evidence-retriever`; decides
when extraction is needed, when evidence retrieval is needed, and when the
answer is ready. Handoffs are explicit and logged; each worker span is a child
of the supervisor span; correlation ID propagates from Week 1 middleware into
every node, VLM call, retrieval call, and FHIR write.

## 6. RAG design — _TBD (Stage 2)_

Small clinical-guideline corpus (ADA Standards of Care, ACC/AHA hypertension,
USPSTF) matched to the demo panel's conditions. Hybrid retrieval: BM25 (sparse)
+ FAISS (dense) candidates → local cross-encoder rerank → top-k evidence only
to the answer model. Patient-record facts and guideline evidence are separated
by **type** (distinct citation shapes), never merged.

## 7. Eval gate — _TBD (Stage 4)_

50 synthetic/demo cases exercising extraction, evidence retrieval, citations,
refusals, and missing-data behavior. Boolean rubrics only: `schema_valid`,
`citation_present`, `factually_consistent`, `safe_refusal`, `no_phi_in_logs`.
A PR-blocking git pre-push hook runs the suite and **fails the build if any
category regresses >5% or drops below its pass threshold**. The golden set lives
in the repo (reproducible without a database).

## 8. Risks & tradeoffs

| Risk | Mitigation |
|---|---|
| VLM hallucinates fields / overstates confidence | strict schema gate + source citation required per field; critic rejects uncited claims |
| PDF bbox overlay needs pixel coords Claude can't emit | generate text-layer PDFs → `pdfplumber` exact boxes; no OCR pipeline |
| OpenEMR FHIR writes may not be supported | **P0 spike** probes write support before building ingestion; REST document-upload fallback |
| PyTorch bloats the Railway image | small model weights only; consider CPU-only wheels |
| Multi-replica breaks in-memory graph/session state | documented single-instance constraint; shared store is hardening-tail work |

## 9. Testing strategy — _TBD_

- **Unit:** schema validators, tool functions, retrieval scoring.
- **Integration (no live API):** full ingestion-to-answer path with fixture
  documents + stubbed LLM/VLM responses.
- **Golden set:** agent behavior (the 50 cases).
- **Not tested / why:** _TBD._

Every test names the failure mode it guards against.

## 10. Failure modes & recovery — _TBD_

Document ingestion failure · extraction schema violation · RAG returns no
results · supervisor routing error — each with "how to spot it in logs" +
recovery action. _TBD as stages land._
```

