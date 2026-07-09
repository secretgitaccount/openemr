# PRP M0-2 · Correlation-ID middleware + structured logging

**Milestone:** M0 · **Depends on:** M0-1 · **Blocks:** (used by all later work)

## Goal
Every request gets a unique correlation ID that appears in every log line, tool call, and (later) LLM/OpenEMR interaction — reconstructable from logs alone (PRD NFR-2, FR-14).

## Context
- PRD NFR-2; the correlation ID is also threaded into OpenEMR's `api_log` later (M1 retrieval).

## Spec
- `logging.py`: configure **structlog** for JSON logs; bind a `correlation_id` from a `contextvars.ContextVar`. Provide `get_logger()`.
- `middleware.py`: ASGI/HTTP middleware that reads an incoming `X-Correlation-ID` header or generates a `uuid4`, sets the contextvar, adds it to the response header, and logs request start/end with method, path, status, duration_ms, correlation_id.
- Wire the middleware in `main.py`.
- Expose a helper `current_correlation_id() -> str`.

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_correlation.py -q
```
Test asserts: a request without the header gets a generated ID echoed in the response header; a request WITH `X-Correlation-ID: abc` echoes `abc`; and a captured log line for that request contains the same `correlation_id`.

## Definition of done
Correlation ID is present on every log line and response; test passes.
