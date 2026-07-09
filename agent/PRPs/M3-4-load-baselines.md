# PRP M3-4 · Load tests (Locust 10/50) + baselines

**Milestone:** M3 · **Depends on:** endpoints · **Needs API key:** no (LLM stubbed for load)

## Goal
Locust load tests at 10 and 50 concurrent users recording p50/p95/p99 latency + error rate, plus captured baseline metrics (PRD §13). **Load tests must not call real Claude** — stub the LLM so throughput is measured without spend (per the cost design).

## Context
- Own new dir only: `loadtest/`. The agent must run in a **stub-LLM mode** for load (an env flag, e.g. `COPILOT_LLM_STUB=1`, that makes `LLMClient` return a canned `GroundedSummary`/`GroundedAnswer` instead of calling Anthropic) — add that flag support in `llm/client.py` **only if not already present** (this is the one non-loadtest file M3-4 may touch; keep the change minimal and covered by a test).

## Spec
- `loadtest/locustfile.py` — a `HttpUser` exercising `/health`, `/ready`, and `POST /patients/{id}/summary` (with `X-Break-Glass-Reason`), weighted realistically.
- `loadtest/run.sh` — headless Locust at 10 and 50 users (`--users 10 --spawn-rate 2 --run-time 1m`, then 50), writing CSVs.
- `loadtest/BASELINES.md` — a template + the recorded p50/p95/p99 + error rate at 10 and 50 users, plus CPU/memory notes and how to reproduce. (The builder fills the numbers it can capture running against the local stub-LLM app; if it can't run Locust in-sandbox, it leaves the command + an empty results table with clear repro steps.)

## Validation
```bash
cd agent && . .venv/bin/activate && python -c "import ast; ast.parse(open('loadtest/locustfile.py').read()); print('locustfile parses')"
# stub-mode unit test:
pytest tests/test_llm_stub.py -q && pytest -q
```
The locustfile parses; the stub-LLM flag returns a canned answer without hitting Anthropic (unit-tested); full suite stays green.

## Definition of done
Runnable Locust scenarios for 10/50 users against a stub-LLM app (zero Claude spend), plus a baselines doc with the recorded/reproducible numbers.
