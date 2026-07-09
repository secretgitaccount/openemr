# PRP M0-3 · /health and /ready endpoints

**Milestone:** M0 · **Depends on:** M0-1 · **Blocks:** none

## Goal
Separate liveness and readiness. `/ready` must validate **meaningful** dependencies, not return 200 unconditionally (PRD FR-15, engineering requirement).

## Spec
- `health.py` with a router:
  - `GET /health` → `{"status":"ok"}`, always 200 if the process is alive.
  - `GET /ready` → checks each dependency and returns per-dependency status + overall 200/503:
    - **OpenEMR API** reachable (HTTP GET the base/`/apis/default/fhir/metadata` capability statement; expect 200/401 = reachable).
    - **Anthropic** configured (API key present; a cheap reachability check or key-present check — do not spend tokens).
    - **Langfuse** reachable (or "not configured" if keys absent — degrade, don't fail hard in dev).
    - **OpenEMR audit globals** assertion hook (stub now; real check reads `api_log_option`/`enable_auditlog` — mark as "assert-on-startup TODO" wired in M1).
- Mount the router in `main.py`.
- Each check has a short timeout and never hangs `/ready`.

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_health.py -q
```
Tests: `/health` → 200; `/ready` returns a JSON object with a key per dependency; with OpenEMR reachable at localhost:8300 the OpenEMR check is `ok`; with a dependency mocked unreachable, `/ready` → 503 and names the failing dependency.

## Definition of done
`/ready` genuinely probes OpenEMR (+ others) and fails loud when a dependency is down.
