# Clinical Co-Pilot agent

A separate Python/FastAPI service that gives an OpenEMR physician a fast,
grounded, cited synthesis of a patient at the point of care. It reads patient
data **only** through OpenEMR's OAuth2 / SMART-on-FHIR API using the logged-in
clinician's token — never the database or service layer.

See `PRD.md` for the full product requirements and `PRPs/` for the atomic
build units. Architecture docs: `../ARCHITECTURE.md` (Week 1) and
`../W2_ARCHITECTURE.md` (Week 2 multimodal + multi-agent additions).

## Week 1 baseline vs. Week 2 (what changed)

**Week 1 (baseline):** a read-only agent over OpenEMR's OAuth2/SMART-FHIR API —
grounded, cited chart summaries with deterministic verification, panel + role
gates, PHI scrubbing, Langfuse observability, and a starter eval suite. Endpoints:
`POST /patients/{id}/summary`, `POST /patients/{id}/conversation`, `/health`,
`/ready`, `/patients`.

**Week 2 (this deliverable) adds two capabilities without forking that core:**

1. **The agent can *see* documents.** It reads clinical documents already in the
   chart (lab PDFs, intake forms uploaded by front desk / nurse / portal),
   extracts strict-schema facts with a Claude vision model, and links every fact
   to a source with a click-to-source **PDF bounding-box overlay**.
2. **The agent *routes* work across a small multi-agent graph.** A LangGraph
   supervisor routes to two workers — `intake_extractor` and `evidence_retriever`
   — with an inspectable handoff log, then answers a grounded question with
   patient-record facts kept separate from retrieved guideline evidence.

Gated by an **eval-driven CI** (50-case golden set, boolean rubrics, PR-blocking
git hook) that blocks regressions. New Week-2 endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /patients/{id}/ask` | **the flagship Week-2 flow** — grounded Q&A over the chart + guidelines |
| `GET /patients/{id}/chart-documents` | list documents already on the chart |
| `POST /patients/{id}/chart-documents/{doc_id}/ingest` | extract a chart document's facts |
| `GET /patients/{id}/chart-documents/{doc_id}/page/{n}` | rendered page image (for the bbox overlay) |
| `POST /patients/{id}/documents` | upload a source document (alternate ingest path) |
| `POST /preview/page` | page render for an uploaded document |

## Layout

```
src/copilot/
  main.py            # FastAPI app factory + router wiring
  config.py          # pydantic-settings configuration
  openemr/           # OAuth2 client + FHIR client + tools (W1)
  schemas/           # Pydantic contracts (source of truth) (W1)
  orchestrator/      # LLM tool-calling controller (W1/M1)
  verification/      # grounding + deterministic rule engine (W1/M1)
  documents/         # W2: ingestion, VLM extraction, strict schemas, chart-read, OpenEMR writes
  rag/               # W2: guideline corpus, hybrid BM25+FAISS index, cross-encoder rerank
  graph/             # W2: LangGraph supervisor + 2 workers, answer assembly
  api/               # W2: /ask (w2flow), chart-documents, preview endpoints
  evals/             # W2: 50-case golden runner + regression gate
  observability.py   # traces, PHI scrubbing, per-encounter EncounterMetrics
  scripts/           # phi_check (fail-closed PHI scan), dump_openapi
tests/               # pytest suite (hermetic: no key/network needed)
```

## Prerequisites

- Python 3.13, with the pre-provisioned virtualenv at `agent/.venv`
  (dependencies from `requirements.txt` are already installed — do **not**
  recreate it).
- A local OpenEMR `development-easy` stack at `http://localhost:8300`
  (OAuth2 password grant enabled; dev creds `admin` / `pass`).

## Configure

```bash
cp .env.example .env      # then fill in real keys as needed
```

Every setting has a development default, so the app boots without a populated
`.env`; production overrides via real environment variables.

## Run locally

```bash
cd agent
. .venv/bin/activate
pip install -e .                              # first time only
uvicorn copilot.main:app --reload
```

Then open the interactive API docs at <http://localhost:8000/docs> (the committed
OpenAPI 3.1 spec is at `agent/openapi.json`).

## Run the core Week 2 flow (no guessing required)

With the agent running and a local OpenEMR stack that has demo patients + at
least one chart document, ask a grounded question about a chart:

```bash
# 1. list documents already on the chart
curl -s localhost:8000/patients/<patient_id>/chart-documents | jq

# 2. ask a grounded question, attaching a chart document by its id
curl -s localhost:8000/patients/<patient_id>/ask \
  -H 'content-type: application/json' \
  -d '{"question":"Is this glucose result concerning per guidelines?",
       "attachments":[{"document_id":"<doc_id>","doc_type":"lab_pdf"}]}' | jq
```

The response separates `record_facts` (from the chart) from `guideline_evidence`
(from RAG), lists only grounded `answer_claims` (ungrounded ones are dropped by
the verification gate), and returns the supervisor `handoffs` log. Every run also
emits a PHI-free per-encounter metrics event (`w2flow.ask.metrics`). A ready-to-run
**Postman collection** is in `agent/postman/` (see its "Week 2" folder).

**Deployed app:** <https://copilot-agent-production-daf5.up.railway.app>
(`/health`, `/ready`, and the Week-2 flow above are live). UI at `/`.

## Test

The suite is **hermetic** — green with no API key, no network, no docker:

```bash
cd agent
.venv/bin/python -m pytest -q          # 455 passed, 2 live-tests deselected
.venv/bin/python -m pytest -m live     # opt-in: needs the live local stack
```

`make ci` (from repo root) runs the full PR-blocking gate — lint → tests → eval
regression gate → fail-closed PHI scan — the same four steps as
`.githooks/pre-push`. Install the hook once with `make install-hooks`.

## Docker

The image (`agent/Dockerfile`) is a slim Python 3.13 base, runs as a non-root
user, binds Railway's injected `$PORT` (falling back to 8000), and has a
`HEALTHCHECK` that hits `/health`.

```bash
cd agent
docker build -t clinical-copilot .
docker run --rm -p 8000:8000 --env-file .env clinical-copilot
# then: curl -sf localhost:8000/health   -> {"status":"ok"}
```

## Deploy (Railway)

The agent deploys as **its own Railway service** (PRD §12/§14). The deploy
artifacts live in `deploy/`:

- `deploy/railway.json` — service config (Dockerfile builder, start command,
  `/health` healthcheck, restart policy).
- `deploy/.env.railway.example` — env template (no secrets) listing required vs.
  optional variables.
- `deploy/RUNBOOK.md` — the operator procedure (`railway init` → set vars →
  `railway up` → verify `/health` + `/ready`).

**Auth boundary:** the agent authenticates to OpenEMR via the OAuth2 *password
grant*, a **dev** setting the stock production OpenEMR image does not enable by
default. A deployed agent must point at an OpenEMR with the password grant
enabled (or use the SMART EHR-launch flow, which is not built in M0–M2). See
`deploy/RUNBOOK.md` → "Auth boundary" before pointing it at production OpenEMR.
