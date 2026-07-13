# W2 Cost & Latency Report (PRP-14, FR-9 / NFR-4 / G5)

Real numbers from **LIVE-LOCAL** runs of the Week-2 multi-agent flow against the
local `development-easy` OpenEMR stack (`http://localhost:8300`) and the live
Anthropic API. Measured 2026-07-13. Harness drove the actual code paths
(`copilot.rag.retrieve`, `copilot.documents.ingest.attach_and_extract`,
`copilot.graph.supervisor.run_graph` → `copilot.graph.answer.build_answer`);
token counts are the model-reported `usage` off each `messages.parse` call. All
OpenEMR records created during measurement were deleted afterwards (verified 0
rows remaining).

## Models in the flow

| Stage | Model | Why |
|---|---|---|
| Document extraction (VLM) | `claude-opus-4-8` | vision/structured extraction of scanned lab PDFs (`VLMExtractor`) |
| Grounded answer synthesis | `claude-sonnet-5` | the configured `ANTHROPIC_MODEL`; grounded-summary synthesis |
| Retrieval (embed + rerank) | `BAAI/bge-small-en-v1.5` + `cross-encoder/ms-marco-MiniLM-L-6-v2` | fully local — **no API cost, no PHI egress** |

Pricing used for estimates (published list price, USD / 1M tokens):
`claude-opus-4-8` = $5 in / $25 out; `claude-sonnet-5` = $3 in / $15 out
(standard rate; Sonnet 5 carries a lower **intro** rate of $2/$10 through
2026-08-31, so the synthesis figures below are a slight over-estimate today).

## Latency — p50 / p95 (measured)

| Operation | n | p50 | p95 | min–max | Notes |
|---|---:|---:|---:|---|---|
| **Retrieval** (BM25+FAISS → RRF → rerank), warm | 20 | **34.5 ms** | **42.7 ms** | 32.7–42.7 ms | fully local; dominated by cross-encoder rerank |
| — retrieval cold model load (one-time) | 1 | 8,294 ms | — | — | embedder + reranker load; pre-fetched into the image (Dockerfile) |
| **Ingestion** (VLM extract + idempotent OpenEMR write) | 3 | **20,530 ms** | **21,674 ms** | 17,520–21,801 ms | one multi-analyte CMP+lipid PDF |
| **Full multi-agent run** (supervisor → evidence_retriever → synth → gate; no attachment) | 6 | **9,698 ms** | **14,368 ms** | 8,835–14,588 ms | retrieval + Sonnet synthesis + verification gate |
| Full run **with** attachment (ingest → retrieve → synth → gate; steps=3) | 1 | 36,740 ms | — | — | end-to-end path; ingestion dominates |

Routing for the full run is inspectable in the handoff log, e.g. with an
attachment: `supervisor→intake_extractor  supervisor→evidence_retriever
supervisor→done` (extracted=1, evidence=4, claims=4).

## Token usage & cost per operation (measured)

| Operation | Model | Input tok | Output tok | Cost / op |
|---|---|---:|---:|---:|
| Document extraction (1 lab PDF) | `claude-opus-4-8` | 3,322 | 1,556 | **$0.0555** |
| Answer synthesis (1 question) | `claude-sonnet-5` | ~2,730 (avg) | ~771 (avg) | **$0.0197** |
| Retrieval (per query) | local | — | — | **$0.0000** |
| Verification gate | deterministic | — | — | **$0.0000** |

Derived per end-to-end **encounter**:

- **Answer only** (question over existing records): ≈ **$0.020** (1 Sonnet call;
  retrieval + gate are free).
- **Answer with 1 ingested document**: ≈ **$0.075** ( $0.0555 VLM + $0.0197
  synth ). Each additional ingested page/document adds ~$0.0555.

> Cost is dominated by the **VLM extraction** (Opus, ~$0.056/doc), which is ~2.8×
> the synthesis call and ~∞× retrieval. Synthesis is the second driver;
> retrieval and the gate are free.

## Actual dev spend (this measurement session)

The PRP-14 build + unit/integration tests run entirely against a **mocked
Anthropic SDK** (no key, no spend — CI-safe). The only spend is this live
acceptance session:

| Calls | Detail | Spend |
|---|---|---:|
| 4 × Opus VLM extract | 3 ingestion iters + 1 with-attachment run | $0.222 |
| 7 × Sonnet synth | 6 full-run iters + 1 with-attachment run | ~$0.14 |
| Retrieval × 40, cleanup | local / DB only | $0.00 |
| **Total live-acceptance dev spend** | | **≈ $0.38** |

## Projected production cost

Assumes each patient encounter = 1 grounded answer, and that ~30% of encounters
also ingest one new document (the rest answer over already-structured records).

| Volume | Answer-only ($0.020) | +30% ingest ($0.0555) | **Blended / encounter** | Monthly |
|---|---:|---:|---:|---:|
| 1 encounter | $0.0197 | — | **$0.036** | — |
| 1,000 encounters | $19.70 | +$16.65 | **$36.35** | — |
| 10,000 / mo | $197 | +$167 | — | **≈ $364 / mo** |
| 50,000 / mo | $985 | +$833 | — | **≈ $1,818 / mo** |

Notes / levers:
- Sonnet 5 **intro pricing** ($2/$10 through 2026-08-31) cuts the synthesis line
  ~33% (answer-only ≈ $0.013), lowering the 10k/mo blend toward ~$300/mo.
- Ingestion cost scales with **document pages**; batching multi-page PDFs into one
  VLM call (already the case) keeps it to one Opus call per document.
- Retrieval and verification stay free at any volume (local models + deterministic
  gate), so cost is ~linear in (answers) + (documents ingested), not in corpus size.

## Bottleneck analysis

1. **Ingestion latency (20.5 s p50) is the dominant wall-clock cost.** It is
   almost entirely the Opus VLM vision call over a multi-page lab PDF; the
   idempotent OpenEMR writes are a small tail. This is the throughput ceiling of
   the full flow and the item to optimize first (e.g. page-parallel extraction,
   a smaller vision model for simple typed PDFs, or async/queued ingestion so the
   answer path isn't blocked on it).
2. **Synthesis (Sonnet, ~9.7 s p50 for the full run) is the second latency
   driver** and the second cost driver. Adaptive thinking accounts for the p95
   spread (8.8 s → 14.6 s). Streaming the answer would cut *perceived* latency
   without changing spend.
3. **Retrieval is negligible** once warm (34.5 ms p50) — BM25+FAISS+rerank over a
   12-chunk guideline corpus. The only retrieval cost is the **one-time cold model
   load (~8 s)**, mitigated two ways: the Dockerfile pre-fetches the weights into
   the image, and `/ready` reports the reranker/vector-index `loaded (warm)` state
   so an orchestrator can gate traffic until warm.
4. **Cost is VLM-bound, not LLM-bound.** Extraction is ~74% of an
   ingest+answer encounter's cost. Answers that don't ingest a document cost only
   the Sonnet call (~$0.02).

## Observability (verified live)

- `langfuse_enabled() == True` in the live run; every graph run opens a single
  `graph.supervisor` span with the worker spans (`graph.intake_extractor`,
  `graph.evidence_retriever`) and the `answer.synthesize` generation **nested
  under one correlation-ID root** (NFR-2). All payloads pass through `scrub_phi`.
- A per-encounter, PHI-scrubbed **`encounter.metrics`** event is emitted
  (`copilot.observability.record_encounter_metrics`) carrying the tool sequence,
  per-step and per-worker latency, token usage + `cost_usd`, routing decisions,
  extraction confidence, claim counts, and the eval outcome — tagged with the run
  correlation ID so it sits alongside the nested spans.
