# PRP M2-5 · Integrate — role gate + multi-turn endpoint + prewarm + cache-through

**Milestone:** M2 · **Depends on:** M2-1, M2-2, M2-3, M2-4 · **Integrator (runs last)** · **Needs API key:** BUILD no (mocked); LIVE acceptance YES

## Goal
Wire M2 into the running service. **This PRP owns all cross-cutting edits** — `orchestrator/controller.py`, `api/*`, `main.py` — so no other M2 PRP touches them.

## Context
Compose: role gate (M2-3) → panel gate (M1-2) → cache-through retrieval (M2-4) → summary (M1) / follow-up (M2-2) → full rule engine (M2-1, already flows via `verify`).

## Spec
- `orchestrator/controller.py` (edit):
  - **Role gate first, before the panel gate:** resolve the acting role (M2-3 `resolve_role`); if not `authorize_clinical_access`, return a refusal envelope (role-denied, logged) with **no** retrieval — mirrors the out-of-panel short-circuit. Order: role → panel → retrieve.
  - Use `cached_critical_set` (M2-4) instead of a raw `get_critical_set` on the request path.
  - Add `answer_followup(conversation_id, question, provider_id) -> ...` to the orchestrator: load the pinned conversation (M2-2), call `LLMClient.answer_followup` with retained context, verify grounding on the answer's claims (reuse M1-6 grounding against the conversation's retained `CriticalSet`), append the turn.
- `api/chat.py` (new): `POST /patients/{patient_id}/conversation` starts a conversation (runs the gated summary, pins the patient, returns a `conversation_id`); `POST /conversations/{conversation_id}/messages` answers a follow-up (cited). Streamed NDJSON like the summary endpoint.
- `api/prewarm.py` (new): `POST /prewarm` triggers `prewarm_schedule` for the provider (background task) and returns the `PrewarmReport`.
- `main.py` (edit): mount the chat + prewarm routers (the only main.py edit in M2).

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_m2_integration.py tests/test_orchestrator.py tests/test_summary_endpoint.py -q   # all mocked — no key
# then full suite:
pytest -q
```
Unit (no key): role-denied identity is refused before retrieval; a follow-up over a started conversation resolves the pinned patient and returns cited claims; a planted interaction surfaces as a flag through the wired summary path; `/prewarm` returns a report.

**LIVE M2 acceptance (needs key + a Front-Office OpenEMR user):** (1) a second follow-up resolves "she" to the current patient without restating; (2) a planted drug-drug interaction is flagged deterministically even when the model omits it; (3) the same patient returns data to the `admin` (physician) token and is **denied** to a Front-Office token.

## Definition of done
Role gate, multi-turn cited follow-ups, the full rule engine, and data-only prewarm all run behind the endpoints; unit tests green without a key; the three live acceptance checks pass with the key. **M2 complete → gate to M3.**
