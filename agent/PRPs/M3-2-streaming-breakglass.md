# PRP M3-2 · Incremental streaming + HTTP break-glass + follow-up deltas

**Milestone:** M3 · **Depends on:** M1-7, M2-5 · **Owns all api/ + controller edits in M3** · **Needs API key:** BUILD no

## Goal
Three endpoint-layer refinements that make the service production-shaped and browser-usable:
1. **True incremental streaming (FR-12):** emit NDJSON events *as each stage finalizes* (headline as soon as the summary parses, each claim as rendered, flags, notices, `data_as_of`) — not compute-then-emit.
2. **HTTP break-glass:** an `X-Break-Glass-Reason` header threads to `patient_summary`/`start_conversation` `break_glass_reason`, so a paneled-by-override request is possible over HTTP (locally, Synthea has no schedule, so this is how the happy path is reachable). Missing/blank header = normal gated path.
3. **Follow-up deltas retention:** retain (or recompute) the deltas for a conversation so "what changed since last visit" follow-ups can ground (today only the plain critical set is retained, so those claims drop). Attach deltas to the conversation's grounding set.

## Context
- Edit `api/summary.py`, `api/chat.py`, `orchestrator/controller.py` only (M3-2 owns all api/controller edits this milestone). Reuse `_provider_id`, `stream_summary`, `HandRolledOrchestrator`, `get_deltas_since_last_visit` (already imported in controller).

## Spec
- Streaming: refactor so the orchestrator can yield stage results progressively (e.g. an async generator variant, or emit each event from the handler as the pieces complete) while keeping the `FhirClient` lifecycle correct. Unit-test that events arrive in order and a refusal still streams a single event.
- Break-glass header: add `X-Break-Glass-Reason` resolution (like `_provider_id`), pass through on the summary + conversation-start endpoints. Log via the existing break-glass audit.
- Follow-up deltas: in `answer_followup`, when the retained critical set has `deltas is None`, recompute via `get_deltas_since_last_visit(patient_id, client=self._fhir)` and attach before grounding (so "what changed" follow-ups ground). Keep it resilient (a delta-fetch failure → empty deltas, not a crash).

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_summary_endpoint.py tests/test_chat_endpoint.py tests/test_m2_integration.py tests/test_streaming.py -q && pytest -q
```
Tests (mocked, no key): events stream incrementally in the correct order; `X-Break-Glass-Reason` reaches the orchestrator and enables the gated path; a "what changed" follow-up grounds against recomputed deltas (claims citing delta records are kept, not dropped). Full suite stays green.

## Definition of done
The endpoints stream progressively, break-glass is reachable over HTTP, and "what changed" follow-ups ground — the service is browser-usable and the M2 follow-up-deltas gap is closed.
