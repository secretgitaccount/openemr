# PRP-14 — Observability + /ready + deploy + cost/latency report

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-10 · **Blocks:** — · **Needs key:** LIVE flow YES

## Goal (atomic)
Make the Week-2 flow observable, ready-checked, deployed, and cost-characterized
(FR-9, NFR-4, G5). Extend — do not fork — the Week-1 observability + health.

## Context / files owned
- `agent/src/copilot/observability.py` (edit), `agent/src/copilot/health.py`
  (edit), `agent/deploy/**`, `W2_COST_LATENCY.md`.

## Contract
- **Per-encounter log/trace** (PHI-scrubbed): tool sequence, latency by step,
  token usage + cost estimate, retrieval hit rate, extraction confidence,
  routing decisions, per-worker latency, eval outcome. Graph spans nest under a
  correlation-ID root in Langfuse.
- **`/ready` extended:** check document storage (OpenEMR write reachable),
  vector index (loaded), reranker (model loadable). Return **degraded** (not
  binary) naming the down dependency.
- **Deploy:** the Week-2 flow live on Railway (clean staged dir, per Week-1
  runbook).
- **`W2_COST_LATENCY.md`:** actual dev spend, projected prod cost, p50/p95 for
  ingestion / retrieval / full multi-agent run, bottleneck analysis.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_health.py tests/test_observability.py -q
```
- `/ready` returns per-dependency status and **degrades** when a dep is stubbed
  down (test), not a binary flip.
- Emitted spans carry correlation_id and nest the graph; a test asserts **no PHI**
  in emitted span/log payloads.
- Cost/latency numbers come from real runs (p50/p95 tables populated).
- **LIVE acceptance:** deployed Railway URL serves `POST /patients/{id}/ask` and
  the document-upload flow end-to-end; Langfuse shows the multi-agent trace.

## Builder prompt (backend-dev → qa)
> Extend `observability.py` to emit per-encounter PHI-scrubbed metrics (tool
> sequence, step latency, tokens+cost, retrieval hit rate, extraction confidence,
> routing decisions, per-worker latency, eval outcome) with graph spans nested
> under a correlation-ID root in Langfuse. Extend `health.py` `/ready` to check
> document storage, vector index, and reranker, returning a **degraded** status
> naming any down dependency. Deploy the Week-2 flow to Railway (clean staged
> dir per the Week-1 runbook). Write `W2_COST_LATENCY.md` with real dev spend,
> projected prod cost, p50/p95 for ingestion/retrieval/full-run, and bottlenecks.
> Test /ready degradation + no-PHI-in-spans. Run the LIVE flow on Railway and
> confirm the Langfuse trace. Hand to qa.
