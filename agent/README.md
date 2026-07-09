# Clinical Co-Pilot agent

A separate Python/FastAPI service that gives an OpenEMR physician a fast,
grounded, cited synthesis of a patient at the point of care. It reads patient
data **only** through OpenEMR's OAuth2 / SMART-on-FHIR API using the logged-in
clinician's token — never the database or service layer.

See `PRD.md` for the full product requirements and `PRPs/` for the atomic
build units.

## Layout

```
src/copilot/
  main.py            # FastAPI app factory + router wiring
  config.py          # pydantic-settings configuration
  openemr/           # OAuth2 client + FHIR client + tools
  schemas/           # Pydantic contracts (source of truth)
  orchestrator/      # LLM tool-calling controller (M1)
  verification/      # grounding + deterministic rule engine (M1)
tests/               # pytest suite
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

Then open the interactive API docs at <http://localhost:8000/docs>.

## Test

```bash
cd agent
. .venv/bin/activate
pytest
```

## Docker

```bash
cd agent
docker build -t clinical-copilot .
docker run --rm -p 8000:8000 --env-file .env clinical-copilot
```
