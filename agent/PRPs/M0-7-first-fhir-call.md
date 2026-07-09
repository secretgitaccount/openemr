# PRP M0-7 · First authenticated FHIR call (M0 acceptance)

**Milestone:** M0 · **Depends on:** M0-4 (oauth), M0-5 (schemas); uses M0-2 (logging), M0-6 (tracing) · **Blocks:** M1

## Goal
Prove the whole foundation end-to-end: get a user token → call OpenEMR's FHIR API *as the user* → return typed data → traced, with a correlation ID throughout. This is the **M0 acceptance criterion**.

## Context
- PRD §14 M0 AC; FR-3, FR-8 (SourceRef), FR-14 (correlation ID into `api_log`).

## Spec
- `openemr/client.py`:
  - `FhirClient` — async httpx client that attaches the bearer token from `TokenProvider`, sets `X-Correlation-ID` on every request (so it lands in OpenEMR's `api_log`), retries transient errors (`tenacity`), and returns parsed JSON.
- `openemr/tools.py`:
  - `get_patient(patient_id) -> ToolResult[Patient]` — `GET {FHIR}/Patient/{id}`, map the FHIR resource to the `Patient` schema, attach a `SourceRef(resource_type="Patient", id=..., timestamp=meta.lastUpdated)`. Wrap in `@trace("get_patient")`.
- Provide a runnable smoke entry: `python -m copilot.openemr.tools --patient <id>`.

## Validation (M0 acceptance)
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
# find a demo patient id from local OpenEMR, then:
python -m copilot.openemr.tools --patient <id>   # prints typed Patient + SourceRef
pytest tests/test_first_fhir_call.py -q          # unit: FHIR->Patient mapping (OpenEMR mocked)
```
Success = a real patient from local OpenEMR is fetched **as the user token**, returned as a typed `Patient` with a valid `SourceRef`, the call appears in Langfuse tagged with the correlation ID, and the same correlation ID is in the agent logs (and sent to OpenEMR's `api_log`).

## Definition of done
One authenticated, typed, traced FHIR read works against the live local OpenEMR. **M0 complete → gate to M1.**
