# PRP M2-3 · Role enforcement (Front-Office vs Physician)

**Milestone:** M2 · **Depends on:** M0-4 (oauth) · **Blocks:** M2-5 · **Needs API key:** no · **Risk:** MED — live role signal (see Validation)

## Goal
Enforce *who is asking* (UC-5, role half): a **Front-Office** identity is denied the clinical chart data a **Physician** receives. This is the role check that complements the patient-panel gate (M1-2) — together they are the "who + which patient" authorization surface.

## Context
- The acting clinician is the OAuth2 user (borrowed identity). Their role must come from OpenEMR, not be asserted by the client.
- Reuse `FhirClient`/`TokenProvider` and `Settings`. Own new file only: `openemr/roles.py`.

## Spec — `openemr/roles.py`
- `Role` enum: `PHYSICIAN`, `FRONT_OFFICE`, `OTHER`.
- `resolve_role(*, client, settings) -> Role` — determine the acting user's role from OpenEMR. Primary signal: the OAuth `userinfo` / current-user identity → the user's OpenEMR ACL group (e.g. "Physicians" vs "Front Office"). If a direct role lookup isn't reachable with the token's scopes, fall back to a configurable dev mapping (`OPENEMR_ROLE_MAP` / per-user setting) — document the production path (PractitionerRole / ACL) in the docstring.
- `authorize_clinical_access(role) -> bool` — `True` only for `PHYSICIAN` (clinician roles); `FRONT_OFFICE`/`OTHER`/unknown → `False`. **Fail closed**: an unresolvable role denies clinical access.
- Emit a structured `copilot.audit.role_denied` event (via `audit.py`, M1-2) when clinical access is denied on role grounds — the agent logs it (OpenEMR won't log a refused call).

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_roles.py -q   # role resolution + fail-closed (OpenEMR mocked)
```
Tests: a physician identity → `PHYSICIAN` → authorized; a front-office identity → `FRONT_OFFICE` → denied + audit event; an unresolvable/unknown role → denied (fail closed). OpenEMR userinfo/role lookup mocked.

**Live note (for the M2-5 acceptance):** proving denial live needs a second OpenEMR user in the Front Office ACL group. The integrator will create/use one (`OPENEMR_FRONT_OFFICE_USER`/`_PASS`) and show the same patient returning data to `admin` (physician) and being denied to the front-office token.

## Definition of done
Role is resolved from OpenEMR (fail-closed on ambiguity), a front-office identity is denied clinical data and the denial is logged, and a physician is authorized — ready for the integrator to wire ahead of the panel gate.
