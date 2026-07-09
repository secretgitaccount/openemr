# PRP M2-2 · Short-lived cache + multi-turn conversation

**Milestone:** M2 · **Depends on:** M1-5 (LLMClient), M1-1 (schemas) · **Blocks:** M2-4, M2-5 · **Needs API key:** to BUILD no (LLM mocked); LIVE follow-up YES

## Goal
Retain the current-patient context across turns (FR-7, UC-3): a follow-up like "what are *her* allergies?" resolves to the patient already in context without restating. The server stays **stateless/horizontally scalable** (NFR-5) by holding conversation state in a **short-lived shared cache**, not in process globals.

## Context
- Reuse `LLMClient` (M1-5), `Claim`/`GroundedSummary` (M1-1 `schemas/output.py`), `CriticalSet`/`Deltas` (M1-1).
- Own new files only: `orchestrator/cache.py`, `orchestrator/conversation.py`, `schemas/conversation.py`, and extend `llm/client.py` (add a follow-up method — do not remove `summarize`).

## Spec
- `orchestrator/cache.py`: `TTLCache` — an async `get`/`set(key, value, ttl)` interface with an in-memory default impl (dict + monotonic expiry). Documented as the swap seam for Redis in production (NFR-5 "short-lived shared cache keeps prewarm coherent across replicas"). No real Redis dependency in M2.
- `schemas/conversation.py`: `ConversationTurn{role: Literal["user","assistant"], text}` and `GroundedAnswer{answer: list[Claim], caveats: list[str]}` (a follow-up answer is still source-bound — reuse `Claim` from `schemas/output.py`).
- `orchestrator/conversation.py`: `ConversationStore(cache)` — `start(patient_id, critical_set_ref) -> conversation_id`, `append(conversation_id, turn)`, `get(conversation_id) -> ConversationState` (holds `patient_id`, turn history, and the retrieved-data reference), TTL-bounded. The patient is pinned at `start`; follow-ups never re-select a patient (safety: a conversation cannot silently pivot to a different chart).
- `llm/client.py` (extend): `answer_followup(question, history, critical_set, deltas) -> GroundedAnswer` — Sonnet, structured output bound to `GroundedAnswer`, given the pinned patient's retained records + prior turns so pronouns resolve. Minimum-necessary PHI; PHI-scrubbed trace `llm.followup`.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_conversation.py -q   # LLM + cache in-memory, mocked — no key
```
Tests: `start` pins a patient; two `append`+`get` calls preserve history across (simulated) separate requests; TTL expiry drops state; `answer_followup` (Anthropic mocked) is called with the pinned patient's records + prior turns and parses a `GroundedAnswer`; a follow-up cannot change the pinned `patient_id`.

## Definition of done
Conversation state lives in a swappable short-lived cache, the patient is pinned for the session, and a mocked follow-up resolves against retained context — the substrate for UC-3 multi-turn, ready for the M2-5 endpoint.
