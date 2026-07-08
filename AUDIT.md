# OpenEMR Audit — Clinical Co-Pilot

Audit of the forked OpenEMR base (Gauntlet-HQ/openemr-base-clean, version 8.2.0-dev) conducted before any AI work, by direct inspection of the codebase and its Docker/schema configuration. Findings are the input to `ARCHITECTURE.md`.

## Key Findings Summary

The most consequential finding of this audit is an **authorization gap, not a bug**: OpenEMR's access control answers "may this user touch this *type* of resource" — never "may this user touch this *patient*." The ACL is action/module-scoped (`AclMain::aclCheckCore($section, $value)`), superuser bypasses every check, and critically, `patient_data.providerID` and the care-team tables exist but the data-access code (`PatientService::search`/`getAll`) does not filter results by the requesting user. Consequently, any authenticated user holding the `patients` ACL can query any patient's chart. The project's headline requirement — "a physician has access to their own patients" — is therefore something the system does **not** provide natively and that the agent layer must build. This single finding reshaped the entire integration plan: it is why the agent is schedule-driven and enforces a patient-panel gate, rather than assuming OpenEMR already scopes access.

Compounding this, the **patient-read path** in the modern Services layer (83 services; e.g. `PatientService`, `AppointmentService`) executes raw SQL with no authorization. A few services embed ACL checks, but the patient-read services do not — and even those checks are the same non-patient-scoped `aclCheckCore($section, $value)`. An agent reading the Services layer or the database directly would inherit zero authorization; OpenEMR's OAuth2/SMART-on-FHIR API is the only path that carries authentication, scope enforcement, and audit logging.

A **second, subtler gap sits in that same API**: the FHIR read path enforces OAuth *scopes* but not OpenEMR's fine-grained sensitivity ACL (`sensitivities/high`) that its UI and write paths apply. On stock OpenEMR a nurse's token and a physician's return identical FHIR results, sensitive records included. We identified this but **scope its remediation out** of the current milestone (closing it means a targeted FHIR-layer change to the core). The agent's multi-user enforcement therefore rests on the patient-panel gate (which we add) plus OpenEMR's **coarse** role gate — which the API *does* enforce (e.g. a Front Office role, lacking chart ACL, is denied). That API, plus a robust `EventAuditLogger` and an existing `BreakglassChecker`, remain genuine assets we build on rather than replace.

On **performance**, the system has no application query/result caching tier by default — Redis exists only in an optional profile, for session storage, not query caching. Every chart read is a cold database hit through legacy PHP on Apache against MariaDB. This bounds the agent's latency budget and is why the design prewarms the day's scheduled patients' data and parallelizes retrieval.

On **security and compliance**, the development build fails several technical safeguards as-run: default credentials (`admin/pass`), HTTP by default (HTTPS available on `:9300` but not enforced), no column-level PHI encryption at rest, and a wide-open dev stack (phpMyAdmin, a mail catcher, LDAP, CouchDB). HTTPS and TLS-for-MariaDB capability both ship (the production profile configures MariaDB with SSL certs), so these are configuration gaps, not missing features. Audit logging of API reads is **verified present by default**. Sending PHI to an LLM introduces a Business Associate Agreement obligation and a PHI-leakage risk through observability traces.

On **data quality**, the panel gate depends on fields that may be incomplete: appointments allow a null patient link (`pc_pid`), and `patient_data.providerID` / care-team data may be missing or stale in demo data. Where panel data is absent, the system must **fail closed (deny)**, not open.

The through-line: **the audit changed the plan.** Reading the code first revealed that authorization must be built, that the API is the only safe integration layer, and that there is no cache to lean on — facts a rush to build would have missed and paid for later.

## 1. Security Audit

### Authentication & authorization

* **ACL is action/module-scoped, not patient-scoped.** `AclMain::aclCheckCore($section, $value)` gates access to resource *types*; there is no native "own patients only." (High impact.)
* **Superuser** (`admin`, `super`) bypasses all ACL checks by design.
* **The patient-read path in the Services layer performs no authorization.** Most services (including `PatientService`/`AppointmentService`) execute raw SQL with no ACL calls; a handful of unrelated services (`EncounterService`, `PatientPortalService`, `FormService`, `FhirLocationService`) embed ACL checks, but those are the same non-patient-scoped `aclCheckCore($section, $value)`. Any code path below the API inherits no patient-level authorization. (High impact.)
* **FHIR read path enforces scopes, not the sensitivity ACL.** Verified: `sensitivities/high` is applied in the legacy UI and on the encounter *write* path, but **never on the FHIR read path** — per-record FHIR filtering is scope-driven only (`ResourceConstraintFilterer`). A nurse's token can retrieve high-sensitivity records a physician's can. Also: patient-context (`patient/`) requests skip even the coarse category gate, so the agent uses `user/` scopes. (High impact; **identified, remediation scoped out** — the agent relies on the patient-panel gate plus OpenEMR's coarse role gate for multi-user enforcement. See `ARCHITECTURE.md` §4.1.)
* **Default credentials** `admin/pass` in the dev build. (High impact, easily fixed.)
* **OAuth2 + SMART-on-FHIR** (`patient/`, `user/`, `system/` scopes) is available for API access — the safe authenticated entry point for the agent. Silent EHR-launch is admin-gated (confidential client + two opt-in flags); access tokens live 1h (use `offline_access` for background refresh).

### Data exposure / transport

* **HTTP served by default** in `development-easy`; HTTPS available (`:9300`) but not enforced.
* **Dev stack exposes** phpMyAdmin (`:8310`), Mailpit, LDAP, and CouchDB — broad attack surface if carried into deployment. (Not present in a hardened single-image production deploy.)

### PHI handling

* **No column-level encryption** of PHI at rest by default; reliance on transport and access control only.
* **New risk introduced by the agent:** PHI egress to the LLM provider, and PHI potentially captured in observability traces.

### Default role model (from the ACL setup)

The shipped role groups and what each can do — relevant because the agent inherits these exactly:

* **Physicians** — full clinical access plus elevated permissions no other role has: high-sensitivity records (`sensitivities/high`), sign lab results (`patients/sign`), code encounters (`encounters/coding`). The attesting clinician.
* **Clinicians** (where a nurse/MA sits) — read/write medical history (`patients/med`), labs, prescriptions, and notes; capped at normal sensitivity; cannot sign or code. This role does the actual chart prep/rooming.
* **Front Office** — calendar (`groups/gcalendar`) and patient report only; no medical-chart access.
* **Accounting** — billing only.
* **Emergency Login** — break-glass; near-superuser, heavily logged.
* **Administrators** — superuser.

**Implication:** role differences are real and concrete at the ACL level, but **never patient-scoped**, and the fine-grained *sensitivity* split is **not enforced on FHIR reads**. We add patient scoping (the panel gate) and rely on OpenEMR's coarse role gate for role-level enforcement (e.g. Front Office is denied charts); closing the FHIR sensitivity gap is identified but scoped out.

## 2. Performance Audit

* **No caching tier by default.** Redis appears only in an optional profile and only as a session store; there is no query/result cache. Every chart read is a cold DB hit. (High impact on agent latency.)
* **Legacy synchronous PHP on Apache**; requests are not async.
* **Large relational schema** — patient context requires joins across `patient_data`, `lists` (problems/allergies/meds), `form_encounter`, `prescriptions`, and `procedure_result`; assembling a full picture is multi-query.
* Data reached via **Doctrine DBAL** (modern) and **ADODB** (legacy), bridged by `DatabaseConnectionFactory::createAdodb`.
* **Note:** runtime baselines (CPU/memory/latency/throughput under load) are not yet measured; they are captured later per the engineering requirements. These findings are from static inspection and bound the design, not final numbers.

## 3. Architecture Audit

* **Two eras coexist:** modern PSR-4 code under `src/` (`OpenEMR\` namespace, Laminas MVC + Symfony components, Twig) and legacy procedural code under `library/` and `interface/` (Smarty, Angular 1.8/jQuery).
* **Data-access layer:** `src/Services/*` — 83 services providing the cleanest, most consistent access, but the patient-read path applies no authorization.
* **API layer:** `apis/` — REST plus SMART-on-FHIR R4/US-Core, OAuth2-protected. The intended integration point.
* **Primary store:** MariaDB. Optional CouchDB for document blobs (scanned files, CCDA) — not core to the agent.
* **Scheduling:** `openemr_postcalendar_events` links patient (`pc_pid`), provider (`pc_aid`), date/time (`pc_eventDate`, `pc_startTime`), and status (`pc_apptstatus`). `AppointmentService` already joins appointments to `patient_data` and provider — the agent can read the day's schedule directly. (Key enabler for the schedule-driven design.)
* **Existing safeguards to build on:** `EventAuditLogger` (`newEvent`, `auditSQLEvent`); `BreakglassChecker` (`isBreakglassUser` — a checker for the emergency-access group, not a full break-glass workflow).
* **Integration points ranked:** API/OAuth (inherits coarse authz + audit — chosen); Services layer (no patient-level authz); direct DB (no authz).

## 4. Data Quality Audit

* **Appointment records permit a null patient link** (`pc_pid` is a nullable `varchar`, not an integer FK) — the schedule may contain slots not yet tied to a patient.
* **`patient_data.providerID` and care-team data** (`care_team_provider`, and the `care_teams` / `care_team_member` tables) may be missing or stale in demo data; because the panel gate depends on these, absent data must cause a **fail-closed (deny)** result, never fail-open.
* **Mixed coded vs. free-text clinical data** — e.g. medications may be free-text (`prescriptions.drug` is free-text; `rxnorm_drugcode` is nullable). A source of agent failure modes and a reason grounding must tolerate imperfect records.
* **Demo-data completeness must be verified per patient** before it can serve as eval ground truth; incomplete records are themselves a required test case (missing ≠ negative finding).

## 5. Compliance & Regulatory Audit

* **Audit logging — verified.** OpenEMR's API access log (`ApiResponseLoggerListener`, gated on `api_log_option`, default Full) records **every remote FHIR/REST request including reads**, writing `log` + `api_log` rows attributed to the OAuth token's user; a generic SQL audit additionally logs whitelisted-table SELECTs (`enable_auditlog`/`audit_events_query`, both default on). **Two caveats we design around:** in-process/local API calls are *not* logged (so the agent must use the remote HTTP API), and the defaults can be disabled (so we assert them at startup and thread our correlation ID into `api_log`).
* **Encryption at rest:** currently absent by default; addressable via MariaDB transparent tablespace encryption (`file_key_management`) + encrypted volume (application-transparent). Treated as expected practice.
* **Transmission security:** TLS capability ships (HTTPS vhost; MariaDB SSL certs in the production profile) but is not enforced in dev — must be enforced in deployment.
* **Data retention & breach notification:** not configured by default; these are organizational obligations the software supports but does not satisfy on its own.
* **BAA implications:** sending PHI to an LLM makes the provider a business associate. Per project rules we assume a signed BAA with no training on data, and use demo data only. Observability backends that could capture PHI fall under the same obligation — hence PHI scrubbing or self-hosting.

**Honest boundary:** the system can be made *compliance-supporting* (technical safeguards in place); full HIPAA compliance additionally requires organizational controls, real key management, risk assessment, and executed BAAs outside the software.
