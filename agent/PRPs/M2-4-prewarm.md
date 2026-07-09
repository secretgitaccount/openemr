# PRP M2-4 · Background prewarm (data-only)

**Milestone:** M2 · **Depends on:** M2-2 (cache), M1-3 (retrieval), M1-2 (schedule) · **Blocks:** M2-5 · **Needs API key:** no

## Goal
Prewarm the day's scheduled patients' **retrieved data** in the background so click-time latency is low, while **LLM synthesis stays at click-time** — so Claude cost is linear in *visits opened*, not schedule size (FR-13). Data-only: prewarm never calls the LLM.

## Context
- Reuse the `TTLCache` interface (M2-2 `orchestrator/cache.py`), `get_critical_set` (M1-3), `get_todays_schedule` (M1-2), `FhirClient`.
- Own new file only: `orchestrator/prewarm.py`. Do not edit the cache or retrieval modules (import them).

## Spec — `orchestrator/prewarm.py`
- `prewarm_schedule(provider_id, *, client, cache, concurrency=4) -> PrewarmReport` — fetch today's schedule, then prefetch each patient's `CriticalSet` concurrently (bounded by `concurrency`), storing each under a stable cache key (e.g. `critical_set:{patient_id}`) with a short TTL. Per-patient failures are recorded, not fatal (the batch always completes).
- `cached_critical_set(patient_id, *, client, cache) -> CriticalSet` — cache-through read used by the request path: return the prewarmed set on hit, else fetch live and populate. (The orchestrator will adopt this in M2-5.)
- `PrewarmReport{scheduled: int, warmed: int, failed: list[str], elapsed_s: float}`.
- Runs under a PHI-scrubbed trace `prewarm_schedule` recording counts/timings only.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_prewarm.py -q   # cache in-memory, FHIR + schedule mocked — no key
```
Tests: prewarm populates the cache for N scheduled patients; `cached_critical_set` returns a hit without a second FHIR fetch; one patient's retrieval failing is recorded in `failed` and does not sink the batch; a cold `cached_critical_set` fetches live then caches. No LLM call anywhere in prewarm.

## Definition of done
The day's schedule can be prewarmed into the shared cache data-only, the request path reads cache-through, and the design keeps LLM cost proportional to visits opened — ready for the integrator to trigger it.
