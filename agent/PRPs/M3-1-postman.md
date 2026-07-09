# PRP M3-1 · Postman collection

**Milestone:** M3 · **Depends on:** endpoints (M1-7, M2-5) · **Needs API key:** no

## Goal
A committed Postman collection covering the core endpoints, runnable without reading source (PRD §13).

## Context
Endpoints (provider via `X-Provider-Id`, default `admin`; break-glass via `X-Break-Glass-Reason` after M3-2): `GET /health`, `GET /ready`, `POST /patients/{patient_id}/summary`, `POST /patients/{patient_id}/conversation`, `POST /conversations/{conversation_id}/messages`, `POST /prewarm`.

## Spec — own `postman/` only
- `postman/clinical-copilot.postman_collection.json` — one request per endpoint, with descriptions, example bodies/headers, a `{{base_url}}` collection variable (default `http://localhost:8000`), a `{{patient_id}}` variable, and an `X-Break-Glass-Reason` header on the summary/conversation requests (so the local happy path works). Capture `conversation_id` from the conversation response into a variable via a test script so the follow-up request chains.
- `postman/clinical-copilot.postman_environment.json` — `base_url`, `patient_id`, `provider_id` variables for local.
- `postman/README.md` — how to import + run (and the `newman` CLI one-liner).

## Validation
```bash
cd agent && python -c "import json,glob; [json.load(open(f)) for f in glob.glob('postman/*.json')]; print('postman JSON valid')"
# optional if newman present: newman run postman/clinical-copilot.postman_collection.json -e postman/clinical-copilot.postman_environment.json --folder health
```
Both JSON files parse; the collection has a request per endpoint with the chaining test script for `conversation_id`.

## Definition of done
A valid, importable Postman collection + environment covering every endpoint, with the conversation→follow-up chain wired, committed under `postman/`.
