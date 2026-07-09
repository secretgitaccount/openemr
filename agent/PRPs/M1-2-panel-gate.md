# PRP M1-2 · Schedule + patient-panel gate (+ agent-side audit)

**Milestone:** M1 · **Depends on:** M1-1, M0 (FhirClient/TokenProvider) · **Blocks:** M1-7 · **Needs API key:** no · **Risk:** MED — requires a paneled patient to exist (see Validation)

## Goal
The gate that runs **before any clinical retrieval** (FR-2). Verify the patient is on the clinician's schedule (or care team); refuse + explain otherwise. Out-of-panel access only via an explicit, logged break-glass reason. The agent logs refusals and break-glass itself — OpenEMR won't auto-log a call that never happened (FR-14).

## Context
- Reuse the built `FhirClient` (`client.get(path, params=)`), `trace(...)`, correlation-id logging.
- FHIR: `Appointment?practitioner=<provider>&date=<today>` (or `Appointment?date=today` filtered to the provider) → today's schedule. Provider = OpenEMR `admin` (dev). Fall back to a recent `Encounter?practitioner=<provider>` if the appointment index is unavailable.
- Own new files only: `openemr/panel.py`, `audit.py`. **Do not edit `openemr/tools.py`.**

## Spec
- `openemr/panel.py`:
  - `get_todays_schedule(provider_id, *, client) -> ToolResult[list[ScheduledPatient]]` (FR-1) — wrapped in `trace("get_todays_schedule")`.
  - `is_patient_in_panel(patient_id, provider_id, *, client) -> ToolResult[PanelDecision]` (FR-2) — `in_panel=True` if the patient is on today's schedule (or has a recent encounter with the provider); else `in_panel=False` with a human reason. Grounds the decision with a `SourceRef` to the Appointment/Encounter when in-panel.
  - `break_glass(patient_id, provider_id, reason, *, client) -> ToolResult[PanelDecision]` — returns `in_panel=True, break_glass=True` and emits an audit event; refuses empty reason.
- `audit.py`:
  - `audit_refusal(patient_id, provider_id, reason)` and `audit_break_glass(patient_id, provider_id, reason)` — structured `structlog` events (`copilot.audit.refusal` / `.break_glass`) carrying the correlation id, PHI-scrubbed (ids + reason, no clinical values). These are the agent-side complement to OpenEMR's `api_log` (FR-14).

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_panel_gate.py -q          # unit: in-panel / out-of-panel / break-glass (FHIR mocked)
# LIVE (dev stack up): ensure one seeded patient is on admin's schedule today, then:
python -m copilot.openemr.panel --provider admin --patient <in_panel_id>    # prints in_panel=True + source
python -m copilot.openemr.panel --provider admin --patient <other_id>       # prints in_panel=False + logged refusal
```
**Seeding note (do this before the live check):** Synthea seeds clinical data but not necessarily an appointment on `admin`'s calendar *today*. Create one appointment (OpenEMR UI: Calendar → add appt for a known patient with provider admin, today) or via the REST `Appointment` endpoint, and treat a different seeded patient as the out-of-panel case. If no schedule exists, the encounter fallback should still panel a patient who has a recent encounter with admin.

## Definition of done
The gate returns a grounded in-panel decision for a scheduled/care-team patient, a clear refusal (logged) for an out-of-panel one, and a logged break-glass override — all before any clinical data is fetched.
