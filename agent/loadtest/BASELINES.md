# Load-test baselines — Clinical Co-Pilot agent (PRP M3-4, PRD §13)

Locust drives the agent's HTTP surface at **10** and **50** concurrent users and
records p50/p95/p99 latency + error rate. All runs use **stub-LLM mode**
(`COPILOT_LLM_STUB=1`) so `LLMClient` returns a canned, source-bound summary
instead of calling Anthropic — **zero Claude spend**, and the numbers reflect the
agent's own throughput (routing, panel gate, verification, NDJSON streaming), not
Anthropic latency. Add a separate live run only when you deliberately want to
include model latency and are willing to pay for it.

## How to reproduce

```bash
cd agent
. .venv/bin/activate
pip install locust                      # if not already installed

# 1. Start the agent in stub-LLM mode (no Anthropic calls).
COPILOT_LLM_STUB=1 uvicorn copilot.main:app --port 8000 &

# 2. (Optional) point the summary task at real local Patient ids.
export COPILOT_PATIENT_IDS="1,2,3"

# 3. Run both scenarios (10 users, then 50) — writes CSVs to loadtest/results/.
./loadtest/run.sh
```

Notes:
- `POST /patients/{id}/summary` sends `X-Break-Glass-Reason` so the granted happy
  path is reachable locally (Synthea patients have no schedule, so the normal
  panel gate would refuse). A summary run needs a **running OpenEMR stack**
  (`docker/development-easy`) for FHIR retrieval; without it the summary task
  returns quickly with `notice`/`refusal` events — `/health` and `/ready`
  throughput are still meaningful.
- Percentiles come straight from Locust's `--csv` output
  (`loadtest/results/users{10,50}_stats.csv`), "Aggregated" row. Locust reports
  p50/p95/p99 columns natively.

## Environment recorded for a run

Fill these in when you capture numbers so the baseline is interpretable.

| Field | Value |
|-------|-------|
| Date | 2026-07-16 |
| Host / CPU / RAM | Apple Silicon Mac (`Mac15,13`), 8 cores, 16 GB |
| Agent revision (git sha) | `2815624` (branch `week2`) |
| Uvicorn workers | 1 (`uvicorn copilot.main:app --host 127.0.0.1 --port 8000`) |
| OpenEMR stack | not running (backend down — so `/ready` and `/patients/*` error; `/health` is unaffected) |
| LLM mode | live process, no LLM calls exercised in this capture (dependency-free GETs only) |
| Run time per scenario | ~50 sequential requests per endpoint (single client) |

## Measured now — sequential single-client latency (dependency-free endpoints)

Captured **2026-07-16** against the live local agent (git `2815624`, 1 uvicorn
worker) with the OpenEMR backend **down**. Method: a single client issues ~50
**sequential** GET requests to each endpoint via Python `urllib` +
`time.perf_counter()`; percentiles are nearest-rank over the per-request wall
times, `RPS` is `count / total_wall_seconds`. This is a *single-client latency
floor*, **not** a concurrency test — the Locust 10/50-user tables below still
need the full stack to fill. `/health` needs no dependencies; `/ready` probes the
(down) OpenEMR OAuth/FHIR surface, so every `/ready` request errors — its timing
is the readiness handler's failure-path latency, recorded here for reference.

| Endpoint | Requests | Failures | p50 (ms) | p95 (ms) | p99 (ms) | min (ms) | max (ms) | RPS |
|----------|---------:|---------:|---------:|---------:|---------:|---------:|---------:|----:|
| GET /health | 50 | 0 | 1.30 | 4.33 | 82.64 | 0.56 | 82.64 | 277.5 |
| GET /ready  | 50 | 50 | 214.72 | 320.48 | 1447.80 | 203.05 | 1447.80 | 3.9 |

`/health` failures: **0** (all `200`). `/ready` failures: **50 / 50** — expected
with the backend down (each returns a not-ready error after probing the
unreachable upstream); the numbers characterise the probe's failure-path latency,
not a healthy readiness check.

Process sampled during the `/health` + `/ready` runs (macOS `ps -o %cpu=,rss=` on
the single uvicorn pid):

| Scenario | Peak CPU % | Peak RSS (MB) |
|----------|-----------:|--------------:|
| sequential single-client (this capture) | ~23.5 | ~332 |

> RSS (~332 MB) reflects the process with the local retrieval models
> (embedder + cross-encoder reranker) loaded into memory.

## Cited from the Week-2 acceptance smoke (LLM/VLM-bound flows)

The ingestion, retrieval, and full `/ask` flows need the running
`development-easy` OpenEMR stack **and** a live Anthropic key, so they are **not**
re-measured here. The figures below are the already-measured numbers from
`W2_COST_LATENCY.md` (LIVE-LOCAL acceptance smoke, measured **2026-07-13**) — cited,
not reproduced. Re-measuring requires bringing the live stack + key back up.

| Operation | n | p50 | p95 | Source |
|---|---:|---:|---:|---|
| Retrieval (BM25+FAISS → RRF → rerank), warm | 20 | 34.5 ms | 42.7 ms | `W2_COST_LATENCY.md` acceptance smoke |
| — retrieval cold model load (one-time) | 1 | 8,294 ms | — | `W2_COST_LATENCY.md` (pre-warmed in image) |
| Ingestion (VLM extract + idempotent OpenEMR write) | 3 | 20,530 ms | 21,674 ms | `W2_COST_LATENCY.md` acceptance smoke |
| Full multi-agent run (supervisor → retrieve → synth → gate; no attachment) | 6 | 9,698 ms | 14,368 ms | `W2_COST_LATENCY.md` acceptance smoke |
| Full run **with** attachment (ingest → retrieve → synth → gate) | 1 | 36,740 ms | — | `W2_COST_LATENCY.md` acceptance smoke |

These are LLM/VLM-bound: retrieval is fully local (free, ~43 ms warm), ingestion is
Opus VLM-dominated (~20 s), and the full run adds Sonnet synthesis + the
verification gate. See `W2_COST_LATENCY.md` for token counts and per-op cost.

## Results — 10 users (`--users 10 --spawn-rate 2 --run-time 1m`)

| Endpoint | Requests | Failures | p50 (ms) | p95 (ms) | p99 (ms) | RPS |
|----------|---------:|---------:|---------:|---------:|---------:|----:|
| GET /health | _ | _ | _ | _ | _ | _ |
| GET /ready | _ | _ | _ | _ | _ | _ |
| POST /patients/{id}/summary | _ | _ | _ | _ | _ | _ |
| **Aggregated** | _ | _ | _ | _ | _ | _ |

Error rate (aggregated): **_ %**

## Results — 50 users (`--users 50 --spawn-rate 5 --run-time 1m`)

| Endpoint | Requests | Failures | p50 (ms) | p95 (ms) | p99 (ms) | RPS |
|----------|---------:|---------:|---------:|---------:|---------:|----:|
| GET /health | _ | _ | _ | _ | _ | _ |
| GET /ready | _ | _ | _ | _ | _ | _ |
| POST /patients/{id}/summary | _ | _ | _ | _ | _ | _ |
| **Aggregated** | _ | _ | _ | _ | _ | _ |

Error rate (aggregated): **_ %**

## CPU / memory notes

Sample `uvicorn` process RSS and CPU% during the 50-user run (e.g. `top -pid
<pid>` on macOS, or `docker stats` if containerised) and record the peak:

| Scenario | Peak CPU % | Peak RSS (MB) |
|----------|-----------:|--------------:|
| 10 users | _ | _ |
| 50 users | _ | _ |

## Status of the in-sandbox capture

The builder that authored this PRP could not run Locust against a live server in
the sandbox (no long-running uvicorn + OpenEMR stack available), so the **10/50-user
Locust results tables are left as a template** with exact repro commands. The
locustfile parses and the stub-LLM flag is unit-tested (`tests/test_llm_stub.py`),
so the scenarios are runnable as-is once an agent process is up.

**Captured 2026-07-16:** the *Measured now* section above records a real
single-client sequential-latency capture against a live local agent (git
`2815624`) for the dependency-free endpoints (`/health`; `/ready` on the
backend-down failure path), plus process CPU/RSS. The LLM/VLM-bound flow figures
(retrieval, ingestion, full `/ask` run) are **cited from `W2_COST_LATENCY.md`**
(LIVE-LOCAL acceptance smoke, 2026-07-13) rather than re-measured, because they
require the running OpenEMR stack + a live Anthropic key. The concurrent Locust
tables and the summary-endpoint rows still need that full stack to fill.
