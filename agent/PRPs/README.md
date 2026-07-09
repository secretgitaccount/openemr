# Atomic PRPs — Clinical Co-Pilot agent

Each file is one **atomic PRP** (Product Requirement Prompt): a single, self-contained unit of work with its own context, spec, and **validation gates**. A builder subagent implements exactly one PRP and must pass its validation before it's considered done. The swarm fans out independent PRPs in parallel and pipelines dependent ones.

**Source of truth:** `agent/PRD.md` (functional requirements FR-#, milestones, acceptance criteria).

## Conventions

- **Package root:** `agent/src/copilot/`. Tests in `agent/tests/`.
- **Env/config:** `agent/.env` (see `.env.example`), loaded via `pydantic-settings`.
- **Python:** 3.13, venv at `agent/.venv` (already provisioned; deps in `requirements.txt`).
- **Local OpenEMR:** `http://localhost:8300` (dev stack running; OAuth2 password grant enabled).
- Every PRP ends with **Validation** = the exact commands/tests that must pass.

## Target module layout (created by M0-1)

```
agent/src/copilot/
  main.py            # FastAPI app + router wiring
  config.py          # pydantic-settings (env)
  logging.py         # structlog config
  middleware.py      # correlation-id middleware
  health.py          # /health, /ready
  observability.py   # Langfuse + PHI-scrub
  openemr/
    oauth.py         # OAuth2 client (register + password grant)
    client.py        # httpx FHIR client
    tools.py         # retrieval tools (M1)
  schemas/
    core.py          # SourceRef, TokenResponse, AgentError, base tool I/O
    patient.py       # Patient, ...
  orchestrator/      # M1
  verification/      # M1
```

## M0 dependency DAG (what the swarm runs)

```
M0-1 scaffold ─┬─> M0-2 correlation-logging ─┐
               ├─> M0-3 health-ready          │ (parallel wave)
               ├─> M0-5 base-schemas ─┐       │
               └─> M0-6 langfuse       │      │
                                       ▼      ▼
                              M0-4 openemr-oauth  (needs scaffold + schemas)
                                       │
                                       ▼
                              M0-7 first-fhir-call  (needs oauth + schemas)  ← M0 acceptance
```

## Milestones (per PRD §14)

- **M0** — Foundation & OAuth validation (these files). Highest-risk unknown first.
- **M1** — Walking skeleton, UC-1/UC-2 end-to-end.
- **M2** — Verification depth + UC-3/UC-4.
- **M3** — Hardening + engineering requirements.

## M1 dependency DAG (what the swarm runs)

Module boundaries are **disjoint by design** so same-wave builders never write the same file. Only M1-7 edits `main.py`; only M1-1 edits `schemas/`; M0's `openemr/tools.py` is left untouched (new tools live in new modules).

```
M1-1 clinical-schemas ─┬─> M1-2 panel-gate ────────────────┐
                       ├─> M1-3 retrieval-tools ─┬──────────┤
                       │                          └─> M1-4 deltas ─┤
                       ├─> M1-5 llm-client (Sonnet) ─────────────────┤
                       └─> M1-6 verification ────────────────────────┤
                                                                      ▼
                                          M1-7 orchestrator + streamed endpoint  ← M1 acceptance
```

New modules: `schemas/clinical.py`, `schemas/output.py`, `openemr/panel.py`, `audit.py`, `openemr/retrieval.py`, `openemr/deltas.py`, `llm/{client,prompts}.py`, `verification/{gate,rules}.py`, `orchestrator/controller.py`, `api/summary.py`.

**API-key boundary:** every PRP **builds and unit-tests with the Anthropic SDK mocked — no key.** The key (`ANTHROPIC_API_KEY` in `agent/.env`, model `claude-sonnet-5`) is needed only for the **live** smokes in M1-5 and the M1-7 acceptance.

## M2 dependency DAG (what the swarm runs)

Only M2-5 edits `controller.py`, `api/`, and `main.py`; each other M2 PRP owns disjoint new modules.

```
M2-1 rule-engine ─────────────┐   (verification/{knowledge,rules,gate})
M2-2 cache+conversation ─┬─────┤   (orchestrator/{cache,conversation}, schemas/conversation, llm follow-up)
M2-3 roles ──────────────┤     │   (openemr/roles)
                          └─> M2-4 prewarm ─┤   (orchestrator/prewarm — needs the cache)
                                             ▼
                              M2-5 integrate  ← M2 acceptance
                              (role gate + chat endpoint + prewarm + cache-through in controller/api/main)
```

**API-key boundary (same as M1):** all M2 PRPs build + unit-test with the LLM mocked — no key. The key (plus a Front-Office OpenEMR user) is needed only for the M2-5 live acceptance.

> M3 PRPs are generated **just-in-time** once M2 clears. The task breakdown is in `PRD.md §14` / §13.
