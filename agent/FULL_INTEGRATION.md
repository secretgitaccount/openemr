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

## Remaining build steps

1. **Agent `/launch` endpoint** — receive OpenEMR's EHR launch (`?launch=&iss=&aud=`),
   run the SMART `authorization_code` handshake, read the **doctor + patient** from
   the launch/token context.
2. **Per-doctor token source** — use that user-bound token for reads instead of the
   fixed `admin` password grant (the token source is already abstracted, so this is
   contained).
3. **Schedule-scoped picker** — populate any patient list from `get_todays_schedule`
   (already built) rather than `GET /Patient`.
4. **Broaden the panel gate** — also honor `Patient.generalPractitioner` / care team,
   not just today's schedule.
5. **Hardening tail** (from DEPLOYED_BUILD.md): `/ready` audit-globals assertion,
   ≥2 replicas, circuit breakers, private networking, verify at-rest encryption.

## Notes / gotchas found

- The SMART card defaults **collapsed** (`getUserSetting('smart')==0`) — cosmetic.
- OpenEMR's launch context carries patient **and** encounter/appointment UUIDs, so
  the launch alone tells the agent which patient to open — no picker needed in the
  embedded flow.
- `skip_ehr_launch_authorization_flow` on the client = the silent-launch flag (no
  consent prompt each launch).
