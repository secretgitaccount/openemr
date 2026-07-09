# PRP M3-6 · Deploy artifacts (agent as its own Railway service)

**Milestone:** M3 · **Depends on:** the app runs (M0-M2) · **Needs API key:** no (artifacts only; the actual `railway up` is run by the operator)

## Goal
Package the agent as its own deployable service and document the deploy (PRD §12/§14: "deployed on Railway as its own service"). This PRP produces the **artifacts + runbook**; the human/operator runs the Railway CLI.

## Context
- A `Dockerfile` already exists at `agent/Dockerfile` — review and finalize it (uvicorn entrypoint, `PORT` env, non-root, `requirements.txt`). Own new dir `deploy/` + the `Dockerfile` edit + a README section.
- **Auth boundary (document clearly):** the agent authenticates to OpenEMR via the **OAuth2 password grant**, which is a **dev** setting (`oauth_password_grant`). The public Railway OpenEMR (production image) does not enable it by default — so a deployed agent pointed at production OpenEMR needs either (a) password grant enabled on that OpenEMR, or (b) the SMART EHR-launch auth-code flow (not built in M0-M2). State this explicitly; do not imply the deployed agent talks to production OpenEMR unconfigured.

## Spec
- Finalize `agent/Dockerfile`: slim Python 3.13 base, install `requirements.txt`, copy `src/`, run `uvicorn copilot.main:app --host 0.0.0.0 --port ${PORT:-8000}`, non-root user, healthcheck hitting `/health`.
- `deploy/railway.json` (or `railway.toml`) — service config: build from the Dockerfile, start command, and the env vars the service needs (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `OPENEMR_*`, `LANGFUSE_*`) documented as required/optional.
- `deploy/RUNBOOK.md` — exact steps: `railway init` / link, set env vars (never commit secrets), `railway up`, verify `/health` + `/ready`; plus the auth-boundary note above and how to point the agent at a password-grant-enabled OpenEMR.
- `deploy/.env.railway.example` — the deployment env template (no secrets).

## Validation
```bash
cd agent && python -c "import json; json.load(open('deploy/railway.json'))" 2>/dev/null || echo "(toml variant)"
docker build -t copilot-agent -f Dockerfile . && docker run --rm -e PORT=8000 -d --name copilot_smoke copilot-agent && sleep 3 && (curl -sf localhost:8000/health || true) ; docker rm -f copilot_smoke 2>/dev/null || true
```
The image builds; the container starts and `/health` responds; `railway.json`/`railway.toml` is valid; the RUNBOOK documents the deploy and the auth boundary.

## Definition of done
A finalized Dockerfile, Railway service config, deploy env template, and a runbook that lets the operator `railway up` the agent and verify it — with the OpenEMR auth boundary documented honestly.
