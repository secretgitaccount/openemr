# PRP M0-5 · Base Pydantic contracts

**Milestone:** M0 · **Depends on:** M0-1 · **Blocks:** M0-4, M0-7

## Goal
Establish the canonical schemas that are the **source of truth** for tool I/O and cross-cutting types (PRD NFR-3). Downstream tools return these; the model's structured output binds to them.

## Spec
- `schemas/core.py`:
  - `SourceRef` — `{ resource_type: str, id: str, timestamp: datetime | None }`. The pointer every clinical claim must carry (grounding).
  - `TokenResponse` — `{ access_token, token_type, expires_in, refresh_token | None, scope }` (used by M0-4).
  - `AgentError` — structured error `{ code, message, retriable: bool }`.
  - `ToolResult[T]` base — `{ data: T, sources: list[SourceRef], retrieved_at: datetime, partial: bool, missing: list[str] }` (carries the "partial answer / what's missing" contract from FR-11).
- `schemas/patient.py`:
  - `Patient` — minimal demographics for M0 (`id`, `name`, `dob`, `sex`, `source: SourceRef`).
- All models: `pydantic v2`, `strict` where sensible, `frozen`/`readonly` for value objects.

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_schemas.py -q
```
Tests: valid sample payloads parse; invalid ones raise `ValidationError`; `SourceRef` round-trips; `ToolResult` carries `partial`/`missing`.

## Definition of done
Schemas import cleanly, validate/reject sample data, and are ready for M0-4 (`TokenResponse`) and M0-7 (`Patient`, `ToolResult`, `SourceRef`).
