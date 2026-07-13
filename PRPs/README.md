# PRPs — Week 2 MVP build units (looping subagent swarm)

Each file is one **atomic PRP** (Product Requirement Prompt): a single,
self-contained unit of work with its own context, spec, and **validation gates**.
This is the same swarm model as Week 1 (`agent/PRPs/`), now with the roles made
explicit as attachable subagents.

**Source of truth:** `../W2_PRD.md` (FR-#, goals, acceptance). Design:
`../W2_ARCHITECTURE.md`.

## The build loop (per PRP)

```
        ┌──────────────────────────────────────────────┐
        ▼                                                │ FAIL (numbered gaps)
  builder subagent  ──build──►  runs own gates  ──►  QA subagent (adversarial)
  (backend-dev /                (pytest, ruff,        re-runs gates + probes
   frontend-dev)                 behavioral)          schema/PHI/key/handoffs
        ▲                                                │ PASS
        └───────────────── loop until green ◄────────────┘  ► PRP done
```

- A **builder** implements exactly one PRP and does not hand off red — it loops
  on its own validation gates first.
- **QA** is independent and adversarial: it re-runs every gate, tries to break
  the contract (malformed input, uncited claims, PHI leakage, tests that
  secretly need a key, cross-PRP file edits), and returns **PASS** or a numbered
  **FAIL** list. Builder fixes each and re-submits. Loop until PASS.

## Roles (subagents in `.claude/agents/`)

| Role | Subagent | Owns |
|---|---|---|
| Back-end dev | `backend-dev` | `agent/src/copilot/**`, `agent/tests/**` |
| Front-end dev | `frontend-dev` | `agent/src/copilot/ui/**` |
| QA | `qa` | verifies any PRP; writes failing tests only |
| Discovery | `Explore` | read-only spikes (PRP-00) |

## Disjoint module ownership (parallel-safe)

Module boundaries are **disjoint by design** so same-wave builders never write
the same file. Each PRP declares **Files owned**. `main.py` and `api/` are
cross-cutting — only one PRP edits them per wave, and those PRPs are placed in
different waves so they never collide.

## Execution waves (what the swarm runs)

```
Wave 0  PRP-00 spike (Explore)          ‖  PRP-01 scaffold (backend-dev)
Wave 1  PRP-02 schemas ‖ PRP-03 demo-docs ‖ PRP-07 corpus            (all backend-dev, disjoint)
Wave 2  PRP-04 VLM-extract ‖ PRP-05 openemr-write ‖ PRP-08 retriever (disjoint)
Wave 3  PRP-06 attach_and_extract + ingestion API                   (edits api/ + main.py)
Wave 4  PRP-09 LangGraph supervisor + 2 workers
Wave 5  PRP-10 grounded answer + citation contract                  (edits api/ + main.py)
Wave 6  PRP-11 golden set ‖ PRP-13 UI overlay (frontend-dev) ‖ PRP-14 obs+deploy
Wave 7  PRP-12 PR-blocking git hook + PHI check         ⭐ graded gate
```

Each wave: builders run in parallel, each followed by its QA loop; the wave
closes only when every PRP in it is QA-PASS. Then the next wave starts.

## API-key boundary (same as Week 1)

Every PRP **builds and unit/integration-tests with the Anthropic SDK
mocked/stubbed — no key, no spend, CI-safe.** The live key (`ANTHROPIC_API_KEY`
in `agent/.env`) is used only at the explicitly-marked **live acceptance**
smokes (real VLM extraction in PRP-06, full flow in PRP-14).

## Index

| PRP | Title | Role | Depends on | Files owned |
|---|---|---|---|---|
| 00 | FHIR-write spike | Explore | — | `PRPs/_spikes/` (probe only) |
| 01 | Deps + package scaffold | backend-dev | — | `requirements.txt`, new empty `documents/ rag/ graph/` |
| 02 | Document schemas + tests | backend-dev | 01 | `documents/schemas.py`, `tests/test_document_schemas.py` |
| 03 | Synthetic demo documents | backend-dev | 01 | `tests/fixtures/documents/**` |
| 04 | VLM extraction + stub test | backend-dev | 02, 03 | `documents/extract.py`, `tests/test_extract.py` |
| 05 | OpenEMR write path | backend-dev | 00, 02 | `documents/openemr_write.py`, `tests/test_openemr_write.py` |
| 06 | attach_and_extract + ingestion API | backend-dev | 04, 05 | `documents/ingest.py`, `api/documents.py`, `main.py`(mount) |
| 07 | Guideline corpus + chunking | backend-dev | 01 | `rag/corpus/**`, `rag/chunk.py` |
| 08 | Hybrid RAG retriever + tests | backend-dev | 07 | `rag/index.py`, `rag/retrieve.py`, `tests/test_rag.py` |
| 09 | LangGraph supervisor + 2 workers | backend-dev | 06, 08 | `graph/state.py`, `graph/supervisor.py`, `graph/workers.py`, `tests/test_graph.py` |
| 10 | Grounded answer + citation contract | backend-dev | 09 | `graph/answer.py`, `api/w2flow.py`, `main.py`(mount) |
| 11 | 50-case golden set + rubrics + runner | backend-dev | 10 | `tests/eval/golden/**`, `evals/w2_runner.py` |
| 12 | PR-blocking git hook + PHI check | backend-dev | 11 | `.githooks/pre-push`, `scripts/phi_check.py`, CI wiring |
| 13 | UI: upload + PDF bbox overlay | frontend-dev | 06, 10 | `ui/index.html`, `ui/**` |
| 14 | Observability + /ready + deploy + cost report | backend-dev | 10 | `observability.py`, `health.py`, `deploy/**`, `W2_COST_LATENCY.md` |
