# FULL INTEGRATION — making the deployed build match the documented target

Phase after the graded submission. Goal: close the gap in `deploy/DEPLOYED_BUILD.md`
so the running system matches ARCHITECTURE.md / PRD.md — headline is **SMART
EHR launch** (per-logged-in-doctor identity), then the §7.4 hardening tail.

Work happens on the **`full-integration`** branch; `main` stays at the
submitted/deployed state.

## Milestone 1 — SMART launch button renders in OpenEMR ✅ VERIFIED (local)

Confirmed on the local dev stack that OpenEMR renders a launch button for the
agent **on the patient chart, with no changes to OpenEMR code** — the SMART-on-FHIR
contract does the integration via app *registration*.

What makes the button appear (from `src/FHIR/SMART/SmartLaunchController.php`):
1. `rest_fhir_api` global is on, **and**
2. a registered OAuth client that is `is_enabled=1` **and** has the **`launch`** scope
   (`getSMARTClients()` filters on exactly these).

How it was verified:
- Registered a client "Clinical Co-Pilot" against local OpenEMR
  (`POST /oauth2/default/registration`) with scopes including `launch launch/patient
  openid fhirUser offline_access user/*.read`, `initiate_login_uri =
  http://localhost:8000/launch`, `redirect_uris = http://localhost:8000/launch/callback`,
  grant types `authorization_code` + `refresh_token`; then set `is_enabled=1`.
- Drove the dev-stack Selenium/Panther browser to log in and open a patient chart
  (`demographics.php?set_pid=1`) — the "SMART Enabled Apps" card renders with a
  **Launch** button labelled **Clinical Co-Pilot**. (Repro script: `tmp/debug.php`.)

## Milestone 2 — the SMART handshake ✅ BUILT + real-stack verified (local)

The button now has a target: the agent performs the full SMART EHR-launch
`authorization_code` handshake and reads as the launched clinician.

New code:
- `copilot/openemr/smart.py` — issuer pinning (host-based), **endpoint discovery**
  (`.well-known/smart-configuration`), authorize-URL builder, and the code→token
  exchange that **keeps the `patient` launch context** (the canonical
  `TokenResponse` drops it). `StaticTokenSource` wraps the clinician token.
- `copilot/smart_session.py` — one-time CSRF launch-state (carries the discovered
  token endpoint across the redirect) + server-side session store (opaque
  HttpOnly cookie → clinician token + patient).
- `copilot/api/launch.py` — `GET /launch` and `GET /launch/callback`.
- `copilot/api/summary.py` — `get_orchestrator` is now **session-aware**: with a
  SMART session, every read + the role gate borrow *that clinician's* token
  (`StaticTokenSource`); otherwise it falls back to the `admin` password grant.
- `ui/index.html` — on `/?patient=<id>` (where the callback lands) auto-opens that
  patient.
- 13 unit/integration tests (`tests/test_smart.py`, `tests/test_launch.py`).

Real-stack verification (agent run locally against the dev OpenEMR):
- `/launch` discovers OpenEMR's advertised endpoints (**https:9300**, not the
  http:8300 read API) and redirects to `/authorize` with correct
  `response_type/client_id/redirect_uri/scope(launch)/state/aud/launch`.
- Real OpenEMR `/authorize` **accepts the client and the `aud`**.
- **✅ Live click-through VERIFIED (2026-07-10):** logged into OpenEMR at
  `https://localhost:9300`, opened a patient, clicked the "Clinical Co-Pilot"
  launch button → agent logs show `launch_started` → `code_exchanged`
  (`has_patient: true, has_refresh: true`) → `session_opened` → the summary loaded
  for the launched patient **as the logged-in clinician**. Browser test needs the
  agent on `localhost:8000` with the SMART client (OPENEMR_CLIENT_ID/SECRET) and
  OpenEMR accessed at its advertised origin (`https://localhost:9300`) so the
  authorize session is same-origin.
- **Known polish:** OpenEMR presents its own OAuth login at authorize (a second
  login). Making it seamless is the **silent launch** refinement
  (`skip_ehr_launch_authorization_flow` / ARCHITECTURE §6) — cosmetic, deferred.

## Remaining build steps

1. **Schedule-scoped picker** — populate any patient list from `get_todays_schedule`
   (already built) rather than `GET /Patient`.
3. **Broaden the panel gate** — also honor `Patient.generalPractitioner` / care team,
   not just today's schedule.
4. **Hardening tail** (from DEPLOYED_BUILD.md): `/ready` audit-globals assertion,
   ≥2 replicas, circuit breakers, private networking, verify at-rest encryption.

## Notes / gotchas found

- The SMART card defaults **collapsed** (`getUserSetting('smart')==0`) — cosmetic.
- OpenEMR's launch context carries patient **and** encounter/appointment UUIDs, so
  the launch alone tells the agent which patient to open — no picker needed in the
  embedded flow.
- `skip_ehr_launch_authorization_flow` on the client = the silent-launch flag (no
  consent prompt each launch).
