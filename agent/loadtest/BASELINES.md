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
| Date | _e.g. 2026-07-09_ |
| Host / CPU / RAM | _e.g. MacBook Pro M-series, 10 cores, 32 GB_ |
| Agent revision (git sha) | _…_ |
| Uvicorn workers | _e.g. 1_ |
| OpenEMR stack | _running / not running_ |
| LLM mode | `COPILOT_LLM_STUB=1` (stub) |
| Run time per scenario | 1m |

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
the sandbox (no long-running uvicorn + OpenEMR stack available), so the results
tables above are left as a template with exact repro commands. The locustfile
parses and the stub-LLM flag is unit-tested (`tests/test_llm_stub.py`), so the
scenarios are runnable as-is once an agent process is up.
