# Clinical Co-Pilot — Architecture

## 1. Summary

The Clinical Co-Pilot is a conversational side panel inside OpenEMR serving one narrow user: a primary care physician who, in the 60–90 seconds before a visit, needs a grounded picture of what changed since the last visit and what matters today. It is not a general medical chatbot; every capability traces to that moment and to a use case in `USERS.md`, or we don't build it.

The decision everything hangs on is **isolation with borrowed identity.** The agent is a separate Python/FastAPI service, not code bolted into the aging PHP, and it reads patient data only through OpenEMR's OAuth2/SMART-on-FHIR API — never the database or internal service layer. Our audit forced this: the patient-read path in the Services layer runs its queries with no authorization, so anything tapping in below the API inherits none. Going through the API as the logged-in clinician means each read carries her token, obeys the coarse role gate OpenEMR *does* enforce there, and lands in the existing audit log — and because the agent never holds a privileged "god" account, it can surface nothing the user couldn't see herself.

But the audit also found a gap the API does **not** close: OpenEMR's permissions are scoped by module and role, never by patient — so we add a **patient-panel gate**: the agent serves only patients on the physician's schedule or care team, and any exception needs an explicit, logged break-glass reason. (The audit also surfaced a second, finer gap — OpenEMR's high-sensitivity ACL is not enforced on the FHIR read path — which we document as a known limitation and scope out of this milestone rather than modify the core; our multi-user guarantees rest on the panel gate plus OpenEMR's coarse, role-level API gate.)

Trust is enforced **structurally, not by asking the model to behave.** Retrieval returns records with IDs and timestamps; the model must bind every clinical claim to a source record, and an unsourced claim never appears as fact. A separate deterministic check — dosage thresholds, interactions, allergy flags — runs in code against the retrieved data and overrides the model. We reject "please cite your sources" (a model that invents a fact invents its citation) and LLM-as-judge in the live path (too slow, itself fallible).

The core tradeoff is **speed versus completeness.** The API is slower than raw SQL with no cache to lean on, so we fetch the critical set in parallel, stream the rest, and prewarm the day's patients' *data* in the background (LLM synthesis stays at click-time) so a click feels instant.

HIPAA shapes everything: TLS on every hop, minimum-necessary data, audit on every access, PHI kept out of traces, and encryption beneath the database — while we're honest this is a compliance-*supporting* posture, not a certificate. And failure is designed for: partial answers over crashes, "no data on file" never mistaken for "none," and graceful degradation to raw records over a confident wrong answer.

## 2. System Components

**Side panel (front end).** A small widget embedded in an OpenEMR page. It shows the logged-in clinician's schedule for the day and, on patient selection, initiates a SMART EHR launch to obtain a user-bound token and calls the agent with that token plus the patient context. It renders the grounded, cited reply and holds no logic beyond display, the SMART launch handshake, and passing context.

**Agent service (Python 3.12 / FastAPI / Uvicorn).** The brain. Responsibilities: receive requests, orchestrate LLM tool-calls, retrieve data through OpenEMR's API, run verification, and stream the answer back. All tool inputs/outputs are strict Pydantic schemas (the contract, not the implementation, is source of truth). Exposes `/health` (process alive) and `/ready` (OpenEMR API, LLM provider, and trace backend reachable; also asserts OpenEMR's audit globals are on — see §4.4).

**OpenEMR (forked, v8.2.0).** Unchanged core. Provides authentication, OAuth2/SMART-on-FHIR APIs, role-based ACL, the calendar/scheduling data, the `EventAuditLogger` and API access log, `BreakglassChecker`, and the MariaDB PHI store.

**Observability backend (Langfuse/LangSmith, PHI-scrubbed).** Captures per-request traces, step timing, tool success/failure, token counts and cost, and verification pass/fail — keyed by correlation ID.

## 3. Request Lifecycle

1. The physician opens a patient at the point of care (her schedule — her own `pc_aid` appointments — is the panel's authorization surface, and the day's patients' data is prewarmed in the background beforehand; prewarming is a system optimization, not a user task).
2. On patient open, the panel performs a **SMART EHR launch** (provider context, `user/*` scopes) to obtain a token bound to the logged-in clinician, then calls the agent with that token and the patient reference. A correlation ID is assigned here and stamped on every downstream log, tool call, and LLM interaction — and passed to OpenEMR so it lands in the API access log for cross-service trace reconstruction.
3. The agent verifies the patient is in the clinician's panel (schedule / care team). If not, it refuses unless a logged break-glass reason is supplied.
4. The orchestrator issues tool calls against OpenEMR's API **as the user, over the remote HTTP FHIR/REST surface** (never in-process, which would bypass both authorization checks and audit logging): today's context, active meds, allergies, recent labs, problem list, deltas since last encounter. Critical fields are fetched synchronously; others deferred.
5. Retrieved records (with IDs + timestamps) are passed to the LLM, which must return structured, source-bound claims.
6. The verification gate checks: every claim has a valid source pointer; deterministic domain rules pass. Unsupported claims are dropped/flagged; rule violations are surfaced.
7. The cited, verified answer streams to the panel with timestamps and any "couldn't retrieve X" notices. OpenEMR's API access log records the access, attributed to the token's user.

### 3a. Trust Boundaries

The system has five distinct trust boundaries, each with its own enforcement mechanism. Nothing is trusted across a boundary until the stated check passes.

1. **Authentication boundary (user ↔ OpenEMR).** The clinician proves identity to OpenEMR's login/OAuth2 flow. The agent never authenticates on its own behalf and never holds a standing privileged credential — it obtains a user-bound token via SMART EHR launch and only ever borrows the authenticated user's identity.
2. **Data-access boundary (agent ↔ OpenEMR).** The primary boundary. Enforced by two layers: (a) the user's OAuth2 token and SMART `user/*` scopes at OpenEMR's API (which carries OpenEMR's coarse, role-level ACL); and (b) our patient-panel gate (schedule/care-team). The agent cannot reach the database or the `Services` layer directly — everything crosses here over HTTP, and out-of-panel access requires a logged break-glass.
3. **Verification boundary (LLM output ↔ clinician).** Model output is treated as untrusted until it passes grounding (every claim maps to a real retrieved record) and deterministic domain-rule checks. Unverified content never reaches the physician as fact.
4. **PHI egress boundary (agent ↔ LLM provider).** The point where data leaves our system. Enforced by minimum-necessary field selection (only what the task needs is sent) and the assumed BAA; TLS in transit.
5. **Observability boundary (agent ↔ trace/log backend).** Enforced by PHI scrubbing — traces and logs carry record IDs and metadata, not clinical values — and/or a self-hosted backend, so monitoring never becomes an unmanaged PHI store.

## 4. How the Design Addresses the Five Hard Problems

### 4.1 Authorization & Access Control

*(Serves UC-5; underpins every other use case.)*

* **Borrowed identity, no god account.** The agent uses the requesting clinician's OAuth token (`user/*` scopes, obtained via SMART EHR launch) for all reads, so it inherits her OpenEMR identity and the coarse role gate OpenEMR enforces at the API — the category-level check of whether a role may touch a resource type at all (e.g. `patients/demo`, `patients/med`, `encounters/auth_a`). We deliberately use **`user/` (provider-context) scopes, not `patient/` scopes**, because OpenEMR skips even the coarse role gate for patient-context requests. The role differences are concrete: the **Physicians** role holds `sensitivities/high`, `patients/sign`, and encounter coding; the **Clinicians** role (nurse/MA) can read/write the chart but is capped at normal sensitivity and cannot sign or code; **Front Office** has calendar and patient-report access only. Because the agent acts as the user, it never re-implements OpenEMR's role model.
* **Known gap: FHIR sensitivity ACL (identified, scoped out).** Our audit found that OpenEMR's fine-grained sensitivity ACL (`sensitivities/high`) is enforced in the legacy UI and on the encounter *write* path, **but never on the FHIR read path** — per-record FHIR filtering is OAuth-scope driven only. So the nurse-vs-physician high-sensitivity split is not enforced by the stock API. Closing it would mean a targeted change in OpenEMR's FHIR layer; we scope that out of this milestone to keep the core unmodified. Our role-level enforcement instead rests on OpenEMR's **coarse** API gate — which does hold (e.g. a Front Office token, lacking chart ACL, cannot read charts, while a physician's can) — plus the patient-panel gate below. UC-5 is demonstrated with the Front-Office-vs-Physician difference and the panel-gate refusal.
* **Patient-panel gate.** Because OpenEMR's ACL is action/module-scoped, not patient-scoped (any authenticated user with the `patients` ACL can query any patient, and superuser bypasses all checks), we add the missing "is this *your* patient?" check. The agent serves only patients on the clinician's schedule (`pc_aid`) or care team (`patient_data.care_team_provider` / the `care_teams` + `care_team_member` tables). The schedule-driven panel makes this the default, not an afterthought.
* **Break-glass.** Out-of-panel access is possible only via an explicit, reason-required override that is loudly audit-logged. This is our own mechanism (the agent records the reason and the access); OpenEMR's `BreakglassChecker` — which only answers `isBreakglassUser(username)` for the emergency-access group — is consulted as one input, not relied on to provide the workflow.
* **Trust boundary.** The single enforcement line is OpenEMR's remote OAuth2 HTTP API. Nothing reaches the DB or `Services` layer except through it — which is also what keeps access auditable (§4.4).

### 4.2 Verification & Trust

*(Serves UC-1, UC-2, UC-4.)*

* **Source attribution by construction.** Tools return structured records with record IDs and timestamps. The model must produce structured output where each clinical statement carries a pointer to the record it came from. A claim with no valid source pointer is never rendered as fact — grounding is enforced by output shape, not by asking the model to behave.
* **Deterministic domain constraints.** Dosage thresholds, drug-drug interaction flags, and allergy contraindications are evaluated in Python against the retrieved data, independent of the model. If the model's summary contradicts a rule, or omits a flag the check found, verification overrides or annotates it.
* **Placement.** A verification gate sits between LLM output and the user; every response passes through it before display.
* **Why not the obvious alternatives.** We do not simply prompt the model to "cite your sources," because a model that hallucinates a fact will just as happily hallucinate a plausible citation for it — self-reported grounding is not grounding. We also do not use an LLM-as-judge in the live path: a second model is slow, adds cost, and is itself fallible, so it cannot be the safety gate. LLM-as-judge is reserved for offline quality grading only. The live gate is deterministic (dictionary lookups and coded rule checks), which is what makes it both trustworthy and fast enough to run on every response.
* **Documented limits.** This catches unsupported/hallucinated claims and rule violations. It is weaker at catching a *wrong interpretation* of data that is technically present, and it does not exercise clinical judgment. These limits are stated, not hidden.

### 4.3 Speed vs. Completeness

*(Serves UC-1, UC-2.)*

* **Explicit latency budget** with first-meaningful-content targeted in ~1–2 seconds.
* **Tiered retrieval.** Critical set (active meds, allergies, recent labs, deltas since last visit) synchronous; remainder deferred or streamed.
* **Background prewarm — data only, synthesis at click-time.** Because OpenEMR has no cache tier and reads are cold, the agent prewarms the *retrieved data* for the day's scheduled patients in the background, so the expensive cold API round-trips are already done when she clicks. LLM synthesis is deliberately **not** prewarmed — it runs at click-time — so we don't pay for tokens on patients who are never opened, which also keeps cost linear in visits, not in schedule size. Cache is short-lived and PHI-aware.
* **Cache coherence under multiple replicas.** The agent runs ≥2 replicas; an in-process prewarm cache would be per-replica (a warm patient on replica A is cold on replica B). We therefore use a short-lived shared cache (e.g. Redis inside the trust boundary, PHI-aware) or sticky routing, and treat a cache miss as a normal cold fetch, not an error.
* **Communicate uncertainty.** Answers show data timestamps ("labs as of yesterday"), indicate what was consulted, and on a slow/failed source return a partial answer with an explicit gap notice rather than hanging.

### 4.4 Data Security & HIPAA

* **In transit:** TLS on every hop (panel↔agent, agent↔OpenEMR, agent↔LLM).
* **Minimum necessary:** the agent selects only required fields and sends the LLM only what the task needs.
* **Auditability (confirmed, and hardened).** OpenEMR logs *every* remote API/FHIR request — reads included — via its API access log (`api_log_option` defaults to Full) plus the generic SQL audit, both attributing the OAuth token's user. Our design turns the default into a guarantee: (a) the agent accesses OpenEMR only over the **remote HTTP API** — in-process/local calls are *not* audited, so this boundary is a correctness requirement, not just a preference; (b) `/ready` asserts `api_log_option >= 1` and `enable_auditlog = 1` at startup; and (c) the agent passes its **correlation ID** to OpenEMR so it lands in the `api_log` row, making a full who/what/when trace reconstructable across both services.
* **Observability is a PHI trap:** traces would otherwise capture chart contents. We redact/scrub PHI from traces and logs (record IDs and metadata, not clinical values) and/or self-host the trace backend inside the BAA boundary, so the monitoring tool never becomes an unmanaged PHI store.
* **BAA assumption:** per project rules, LLM providers are treated as BAA-covered with no training on data; only demo data is used.
* **Residual risk (documented):** stock OpenEMR has no column-level PHI encryption at rest; we address at-rest protection at the storage layer (§5.3) and flag column-level encryption as a deliberately-rejected alternative.

### 4.5 Failure Modes

* **Tool failure / OpenEMR timeout:** return a partial answer that explicitly names what could not be retrieved; never crash, never silently drop. `/ready` reports dependency health.
* **Incomplete record:** the agent distinguishes "no allergy data on file" from "no known allergies." Absence of data is never treated as a negative finding — this is a patient-safety invariant and a dedicated eval case.
* **Unexpected model output:** Pydantic rejects malformed structured output; the verification gate catches unsupported claims. On failure the agent **degrades gracefully** — showing the raw retrieved records ("here is the medication list; I could not safely summarize it") rather than emitting a confident wrong answer. It never fabricates to fill a gap.
* **Predictable under load:** timeouts, retries with backoff, and fail-loud (visible error) over fail-silent, verified by load tests at 10 and 50 concurrent users. Note the scaling ceiling is OpenEMR, not the agent (§6).

## 5. Security, Encryption & HIPAA Posture

### 5.1 What "compliant" actually means here

No software is "HIPAA compliant" on its own. HIPAA compliance is a property of the operating organization — it requires administrative safeguards (policies, risk assessments, training), physical safeguards, technical safeguards, and signed Business Associate Agreements. Software can only be **compliance-capable**: it can provide the *technical safeguards* and be deployed in a way that does not undermine the rest. Our goal is therefore precise — make every HIPAA Security Rule technical safeguard present and correct across both OpenEMR and the agent, and document the org-level controls that remain outside the software. This project uses demo data only and assumes a BAA with the LLM provider, so no real PHI is ever in play; the compliance work is an architectural demonstration, not a legal claim.

The four technical safeguards, and how each is met:

* **Access control** — the agent acts as the logged-in clinician (never a privileged service account), inherits OpenEMR's coarse role gate, and adds the patient-panel gate (§4.1). (The finer FHIR sensitivity ACL is a known gap, scoped out — see §4.1.)
* **Audit controls** — every remote API access is logged and user-attributed by default; we assert the enabling globals and thread the correlation ID through (§4.4).
* **Transmission security** — TLS on every hop (§5.2).
* **Encryption at rest** — MariaDB transparent tablespace encryption plus an encrypted volume (§5.3).

We claim a *compliance-supporting technical posture*, never that the system "is HIPAA compliant."

### 5.2 HTTPS: capability present, simply not enforced in the dev build

OpenEMR already ships full HTTPS capability — the development stack even exposes an HTTPS endpoint (`https://localhost:9300`) alongside the plain-HTTP one, and the production compose profile ships MariaDB configured with SSL certificates (`--ssl-ca`, `--ssl-cert`, `--ssl-key`). The reason transport is currently unencrypted is only that the *development-easy* stack defaults to HTTP for convenience; nothing about the application prevents TLS. This is a configuration gap, not a missing feature — an important distinction for the audit.

In the deployed environment we enforce it on all three hops:

* **Browser ↔ OpenEMR:** served over HTTPS (the platform's managed TLS terminates certificates at the edge and proxies to OpenEMR; the image serves HTTP internally on port 80 and does not force a redirect, so there is no redirect loop behind the proxy).
* **Agent ↔ OpenEMR:** the agent calls the OAuth2/FHIR API over HTTPS only, and the OAuth token itself is only issued over TLS.
* **Agent ↔ database / LLM:** the DB connection uses the SSL certs the production profile provides; LLM calls are HTTPS by default.

So the audit finding is stated honestly: *transport encryption is available and required in deployment; the dev build ships it disabled for local convenience.*

### 5.3 Encryption at rest: MariaDB transparent encryption, invisible to the app and the agent

The key design principle is to encrypt **below** the application, at the storage-engine layer, so that neither OpenEMR nor the agent needs any change and nothing can break.

**Mechanism.** MariaDB (v11.8 in the stack) supports transparent data-at-rest encryption. We enable the `file_key_management` encryption plugin, point it at an encryption-key file supplied as a secret, and turn on tablespace and log encryption (`innodb_encrypt_tables=ON`, `innodb_encrypt_log=ON`, `innodb_encryption_threads` set). MariaDB then encrypts the InnoDB data files and redo logs on disk and decrypts transparently on read. This is a database-configuration change made in the Docker/compose layer — **no application code is touched.** (Note: this requires deploying a self-hosted MariaDB service we control, not a managed MySQL offering — see §7.4.)

**Why it works flawlessly with OpenEMR *and* the agent.** Encryption happens at the storage engine, entirely beneath the SQL interface. OpenEMR issues exactly the same queries and receives exactly the same plaintext result sets — it has no awareness that the files underneath are encrypted, so searching, sorting, reporting, and every existing feature behave identically. The agent is insulated twice over: it never touches the database directly at all (it reads only through OpenEMR's OAuth API), so it is even further removed from the encryption layer than OpenEMR is. There is no code path in either component that can be affected. This is precisely why we **reject column-level encryption inside OpenEMR**: encrypting individual PHI columns would force changes to the data-access layer and break search/sort/report across a large unfamiliar codebase, for no additional safeguard beyond what storage-level encryption already provides. OpenEMR's own `CryptoGen` utility remains reserved for its narrow existing uses (documents, stored secrets), not blanket field encryption.

We also enable disk/volume encryption on the persistent volume holding the database, so the protection holds even if the underlying storage is snapshotted or moved.

**The honest hard part — key management.** Transparent encryption is only as strong as the protection of its key. A key file sitting on the same host as the database it unlocks is weak protection. For this demo the key is supplied as a Docker secret (never baked into an image, never committed to git); we document explicitly that a production deployment would hold the key in a dedicated KMS/HSM outside the database host, with rotation and access logging. Naming this boundary is deliberate: it shows the encryption is real, and that we understand where its guarantees end.

### 5.4 Minimum necessary & the observability PHI trap

Two further safeguards the agent owns directly: it selects only the fields a task requires (minimum necessary), and it prevents observability from silently becoming an unmanaged PHI store — traces and logs record metadata and record IDs, not clinical values, and/or the trace backend is self-hosted inside the BAA boundary.

### 5.5 The residual boundary

What the software cannot supply: organizational policies, workforce training, a formal risk assessment, real KMS-backed key management, and executed BAAs. These remain the deploying organization's responsibility, and we state that rather than implying the architecture closes them.

## 6. Known Tradeoffs & Limitations

* **API front door is slower than direct SQL.** Accepted deliberately for authorization, audit, and a stable contract; mitigated by tiered retrieval and prefetch.
* **A separate service adds operational surface.** Accepted for LLM isolation, modern tooling (eval/observability/schemas), and keeping the EHR untouched.
* **The panel gate depends on schedule/care-team data quality.** If `pc_aid`/care-team data is missing or stale, panel scoping degrades — a direct dependency surfaced by the Data Quality audit and a reason to fail closed (deny) rather than open.
* **The deterministic rule engine has bounded coverage.** It enforces the rules we encode, not all of clinical medicine; scope and gaps are documented.
* **Verification cannot validate interpretation or judgment** — only sourcing and encoded constraints.
* **FHIR sensitivity ACL is a known, un-remediated gap.** OpenEMR's high-sensitivity filter is not applied on FHIR reads; we identified it and scoped the fix out of this milestone (it would require a core change). Multi-user enforcement rests on the panel gate plus the coarse role gate instead.
* **OpenEMR is your scaling ceiling, not the agent.** The agent is stateless and scales horizontally, but every read is a cold, un-cached hit against legacy PHP on Apache over a single MariaDB. At a 500-bed / ~300-concurrent scale the constraint is OpenEMR, not the agent — mitigations are read replicas, a caching layer in front of the API, and connection pooling. Our 10/50-user load tests establish the agent-side baseline; the OpenEMR ceiling is called out explicitly rather than hidden.
* **Silent SMART launch is admin-gated and cookie-sensitive.** The user-bound token flow is a real SMART EHR-launch `authorization_code` round-trip (not a session token exchange). Making it consent-free requires a pre-registered confidential client, admin enablement (`user/*` scopes always require manual approval), and two opt-in flags; access tokens live 1 hour, so background prewarm requests `offline_access` for a refresh token. The silent flow depends on first-party cookies, so an embedded side panel should be same-origin (or use the autosubmit workaround) to avoid third-party-cookie partitioning.

## 7. Technology Stack — What We Use and Why

Every technology below is chosen against the same principle: the agent is I/O-bound (it waits on the LLM and on OpenEMR) and schema-driven (contracts must be enforceable), so we favor an async, strongly-typed, ecosystem-rich stack over raw compute speed.

### 7.1 Application / agent

* **Python 3.12** — the language of the entire LLM/agent/eval/observability ecosystem. Because the agent does little heavy computation and spends its time awaiting network calls, a "faster" compiled language would buy negligible latency while costing us that ecosystem. Chosen deliberately, with the tradeoff named.
* **FastAPI** — the agent's web framework. Async-native (handles many concurrent I/O-bound requests, which is what the 10/50-user load tests exercise), Pydantic-native (validation built in), and it auto-generates an OpenAPI spec that feeds the required Postman/Bruno collection. It also makes the operational requirements trivial: separate `/health` and `/ready` endpoints, correlation-ID middleware, and streaming responses.
* **Uvicorn** — the ASGI server that runs FastAPI in production, providing the async event loop.
* **Pydantic v2** — strict schemas for every tool input/output and API contract. The contracts, not the implementation, are the source of truth, satisfying the "canonical schema" engineering requirement; it also rejects malformed model output as part of failure handling.
* **LLM SDK (Anthropic/OpenAI) with tool-calling** — the reasoning engine. We use native tool/function-calling to keep the model on rails (it requests structured data through defined tools rather than free-associating). A light orchestration layer (e.g. LangGraph or a hand-rolled controller) sequences multi-step calls; kept minimal so behavior stays defensible.
* **httpx (async)** — the HTTP client the agent uses to call OpenEMR's OAuth2 FHIR/REST API. Async so the critical-data tool calls run in parallel, not in series.

### 7.2 Data & the EHR (existing, integrated with)

* **OpenEMR 8.2.0 (forked)** — the EHR we embed into; provides authentication, the OAuth2/SMART-on-FHIR API, role-based ACL, scheduling data, `EventAuditLogger` and API access log, `BreakglassChecker`, and the PHI store. Untouched at its core.
* **OAuth2 / SMART-on-FHIR** — the authorization and data-access protocol. The agent obtains a user-bound token via SMART EHR launch (provider context, `user/*` + `offline_access` scopes) and reads through it; the token carries the user's identity and scopes on every request.
* **MariaDB 11.8** — OpenEMR's primary data store (MySQL-compatible), reached by OpenEMR via Doctrine DBAL / ADODB. The agent never touches it directly. We self-host it (not a managed MySQL) so we can enable transparent tablespace encryption for at-rest protection (§5.3).

### 7.3 Trust, observability & testing

* **Deterministic verification engine (Python)** — in-process grounding checks and coded clinical rules; not a library choice but a first-class component, and deliberately *not* an LLM.
* **Langfuse or LangSmith (PHI-scrubbed)** — tracing/observability: per-request traces, step latency, tool success/failure, token/cost, and verification pass/fail, keyed by correlation ID. Scrubbed or self-hosted so it never becomes an unmanaged PHI store.
* **pytest** — the eval/test harness that runs the boundary, invariant, and regression cases in CI (including UC-5's role-gate test — a Front-Office token denied chart data a Physician token receives — and the panel-gate refusal test).

### 7.4 Infrastructure

* **Docker** — every component is containerized for parity between local and deployed environments.
* **Local: `docker/development-easy` + agent container** — OpenEMR + MariaDB come up via the fork's built-in compose stack (`localhost:8300`); the agent runs alongside (containerized, or `uvicorn --reload` for fast iteration) pointed at the local OpenEMR API, with secrets in `.env`. "You cannot audit or build what you cannot run."
* **Deployed: Railway** — one project, three services (OpenEMR, **self-hosted MariaDB**, agent), joined by private networking (IPv6 — the DB binds `::`) and cross-service variable references, with public domains for OpenEMR and the agent. The database uses a **persistent, encrypted volume** with `file_key_management` (§5.3); the OpenEMR `sites/` directory is on its own persistent volume so redeploys don't re-run setup. TLS is enforced on all hops. Deploys run via the Railway CLI (`railway up`) — manually or through a GitLab CI job — since Railway doesn't natively watch self-hosted GitLab. Known risk: OpenEMR's image expects a first-boot setup/persistence step and is the fiddly part of a Railway deploy (a managed MySQL is easier but cannot run `file_key_management`, which is why we self-host MariaDB). Documented fallback: run OpenEMR + MariaDB on a small VM and deploy only the agent on Railway.
* **Resilience primitives** — the agent is stateless (crash-safe, horizontally scalable), runs behind the platform load balancer with ≥2 replicas, auto-restarts on failure, and uses `/health` (liveness) and `/ready` (dependency reachability + audit-globals assertion) so unhealthy instances stop receiving traffic. Circuit breakers on OpenEMR and the LLM prevent a failing dependency from cascading. A short-lived shared cache (§4.3) keeps prewarm coherent across replicas.
