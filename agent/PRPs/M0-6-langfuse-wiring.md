# PRP M0-6 · Langfuse observability wiring

**Milestone:** M0 · **Depends on:** M0-1 · **Blocks:** none (used by M0-7 + all later)

## Goal
Stand up PHI-scrubbed tracing so every request/tool/LLM step is observable, keyed by correlation ID (PRD §7.3, NFR-4).

## Spec
- `observability.py`:
  - Initialize the Langfuse client from env; **degrade gracefully** to a no-op if keys are absent (dev must still run).
  - `trace(name)` context manager / decorator that opens a Langfuse span, tags it with the current `correlation_id`, records duration, success/failure, and (later) token counts.
  - `scrub_phi(obj) -> obj` — redacts clinical **values** but keeps record **IDs**, resource types, and metadata (NFR-4). Provide an allowlist of safe keys.
  - Helper to record a "verification pass/fail" event and "tool success/fail" (used by the dashboard in M3).
- Never send raw PHI to Langfuse — all payloads pass through `scrub_phi`.

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_observability.py -q
```
Tests: with no Langfuse keys, `trace()` is a no-op and code still runs; `scrub_phi({"patient_id":"123","lab_value":"K 5.9"})` keeps `patient_id`, redacts `lab_value`; a traced block records a span with the active correlation ID (Langfuse client mocked).

## Definition of done
Tracing works when configured, no-ops when not, and PHI never leaves via traces.
