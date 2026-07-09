# PRP M1-7 · Orchestrator + streamed cited endpoint (M1 acceptance)

**Milestone:** M1 · **Depends on:** M1-2, M1-3, M1-4, M1-5, M1-6 · **Integrator (runs last)** · **Needs API key:** to BUILD no (LLM mocked); for the LIVE acceptance YES

## Goal
Wire the walking skeleton end-to-end and expose it. The hand-rolled orchestrator (swappable `Orchestrator` interface, PRD §12) runs the full request lifecycle; a streamed FastAPI endpoint returns a grounded, cited answer with data timestamps and explicit "couldn't retrieve X" notices (FR-12), degrading gracefully (FR-11). **This PRP owns the `main.py` router edit — no other PRP touches `main.py`.**

## Context
- Compose the built pieces: panel gate (M1-2) → parallel critical-set + deltas (M1-3/M1-4) → LLM synthesis (M1-5) → verification gate (M1-6) → streamed cited response.
- Own new files: `orchestrator/controller.py`, `api/__init__.py`, `api/summary.py`; edit `main.py` to mount the router.

## Spec
- `orchestrator/controller.py`:
  - `class Orchestrator(Protocol)` with `async def patient_summary(patient_id, provider_id) -> VerifiedSummary` (+ the partial/missing envelope).
  - `class HandRolledOrchestrator` implementing it:
    1. **Gate first** (FR-2): `is_patient_in_panel`; if not in panel (and no break-glass), **stop before any retrieval**, log the refusal (M1-2 audit), return a refusal envelope. 
    2. Parallel `get_critical_set` + `get_deltas_since_last_visit` (FR-4).
    3. `LLMClient.summarize(...)` (FR-8).
    4. `verify(...)` (FR-10) — drop ungrounded claims, attach rule flags.
    5. Return `VerifiedSummary` + `missing`/timestamps for the FR-12 notices.
  - Whole flow under one correlation id + a parent `trace("patient_summary")`.
- `api/summary.py`:
  - `POST /patients/{patient_id}/summary` (provider from auth context; dev = `admin`) → **streamed** response (`StreamingResponse`): headline/must-knows/what-changed as they finalize, each claim rendered with its `source_id`(s), a "data as of <ts>" line, and any "couldn't retrieve X" notices. An out-of-panel patient streams a single refusal payload.
- `main.py`: mount the `api/summary.py` router.

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_orchestrator.py tests/test_summary_endpoint.py -q   # LLM + FHIR mocked — NO key needed
# LIVE M1 ACCEPTANCE (needs ANTHROPIC_API_KEY in agent/.env, dev stack up, a paneled patient seeded):
uvicorn copilot.main:app --port 8000 &            # or the project's run cmd
curl -N -X POST localhost:8000/patients/<in_panel_id>/summary   # streamed grounded+cited "what changed + must-knows"
curl -N -X POST localhost:8000/patients/<out_of_panel_id>/summary   # streamed refusal, logged
```
**M1 acceptance (PRD §14):** a paneled patient returns a grounded, cited "what changed + must-knows" summary; an out-of-panel patient is refused and logged; a missing field reports "no data on file" not "none"; every rendered claim links to a `source_id`. Unit tests prove the wiring **without** a key; the live curl proves it end-to-end **with** the key.

## Definition of done
The full M1 lifecycle runs behind one streamed endpoint: gate → parallel retrieval → Sonnet synthesis → verification → cited stream, with refusals logged and graceful degradation — the walking skeleton is testable end-to-end. **M1 complete → gate to M2.**
