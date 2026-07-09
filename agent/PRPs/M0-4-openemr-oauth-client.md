# PRP M0-4 · OpenEMR OAuth2 client (the risky one)

**Milestone:** M0 · **Depends on:** M0-1, M0-5 · **Blocks:** M0-7 · **Risk:** HIGH — validate against the live local OpenEMR

## Goal
Obtain a **user-bound** OpenEMR access token the agent can use to call the FHIR API *as the logged-in clinician* (borrowed identity, PRD FR-3). This is the foundation the whole agent sits on — prove it works before anything downstream.

## Context
- Local `development-easy` enables the **OAuth2 password grant** (`oauth_password_grant: 3`) and REST/FHIR APIs. Use password grant for dev to get a real user token without the SMART EHR-launch UI. (Production embedded panel uses SMART EHR launch — document, don't build here.)
- OpenEMR OAuth2 endpoints under `OPENEMR_OAUTH_BASE` (`/oauth2/default`): `/registration`, `/token`. FHIR under `OPENEMR_FHIR_BASE`.
- Scopes: request `openid offline_access api:fhir user/*.read` (provider/user context — **not** `patient/`, which skips even the coarse gate).

## Spec
- `openemr/oauth.py`:
  - `register_client()` — dynamic client registration (`POST /registration`) if `OPENEMR_CLIENT_ID` is empty; persist the returned `client_id`/`client_secret` (write back to `.env` or a cache file). Idempotent — reuse if present.
  - `get_user_token(username, password) -> TokenResponse` — password grant (`POST /token`, `grant_type=password`, client auth, scopes above). Returns access token, refresh token, expiry (use the `TokenResponse` schema from M0-5).
  - `TokenProvider` — caches the token, refreshes with the refresh token before expiry (`tenacity` retry on transient failures). Thread the correlation ID into request logs.
- Handle OpenEMR's self-signed TLS if HTTPS is used; dev is HTTP on 8300.
- Clear errors (never leak secrets in logs).

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
# Live check against local OpenEMR (dev stack must be up on :8300):
python -m copilot.openemr.oauth --smoke     # registers client + gets a token, prints token type + scope (NOT the token)
pytest tests/test_oauth.py -q                # unit: token parsing, refresh logic (OpenEMR mocked)
```
Smoke must print a valid `access_token` acquired (masked) with `user/*.read` scope. If password grant is unexpectedly disabled, fall back to documenting the SMART auth-code flow and mark this PRP **blocked** for human setup — do not fake success.

## Definition of done
The agent can obtain and refresh a real user-bound token from the local OpenEMR. This is the M0 make-or-break; if it can't, we stop and fix before building M1.
