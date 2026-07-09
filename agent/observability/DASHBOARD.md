# Clinical Co-Pilot — Langfuse Dashboard Spec (M3-5)

**Status:** committed specification + config. Langfuse is not running in the dev
sandbox, so this file defines the dashboard against the trace/event names the
agent *actually emits*. Every panel below cites its source span or event and the
derivation. No metric references a name that does not exist in `src/copilot`.

## Where the telemetry comes from

All observability is wired through `src/copilot/observability.py`:

- `trace(name, ...)` opens a Langfuse span, records `duration_ms` and a
  `success: bool` in span metadata, and on exception marks the span
  `level="ERROR"` with `status_message = type(exc).__name__` (exception *type*
  only — never the message, which could carry PHI). Every span also carries the
  active `correlation_id`.
- `record_verification(passed, metadata=...)` emits a `verification.pass` /
  `verification.fail` event (called from `verification/gate.py`).
- `record_tool_result(tool, success, ...)` emits `tool.success` / `tool.fail`
  events (helper present; see "Tool metrics" note below on the current source).
- All payloads pass through `scrub_phi()` before leaving the process.

### Span names emitted (verified via grep of `src/copilot`)

| Span | Source file | Metadata keys (scrubbed) |
|------|-------------|--------------------------|
| `patient_summary` | `orchestrator/controller.py` | `patient_id`, `provider_id`, `success`, `duration_ms` |
| `answer_followup` | `orchestrator/controller.py` | `conversation_id`, `provider_id`, `success`, `duration_ms` |
| `get_critical_set` | `openemr/retrieval.py` | `resource_type`, `patient_id`, `success`, `duration_ms` |
| `get_deltas_since_last_visit` | `openemr/deltas.py` | `resource_type`, `patient_id`, `success`, `duration_ms` |
| `get_patient` | `openemr/tools.py` | `resource_type`, `patient_id`, `success`, `duration_ms` |
| `get_todays_schedule` | `openemr/panel.py` | `provider_id`, `success`, `duration_ms` |
| `is_patient_in_panel` | `openemr/panel.py` | `patient_id`, `provider_id`, `success`, `duration_ms` |
| `break_glass` | `openemr/panel.py` | `patient_id`, `provider_id`, `success`, `duration_ms` |
| `resolve_role` | `openemr/roles.py` | `success`, `duration_ms` |
| `prewarm_schedule` | `orchestrator/prewarm.py` | `provider_id`, `success`, `duration_ms` |
| `llm.summarize` | `llm/client.py` | `model`, `success`, `duration_ms` |
| `llm.followup` | `llm/client.py` | `model`, `success`, `duration_ms` |
| `verify` | `verification/gate.py` | `resource_type`, `kept`, `dropped`, `flags`, `success`, `duration_ms` |

### Events emitted

| Event | Source | Metadata keys (scrubbed) |
|-------|--------|--------------------------|
| `verification.pass` / `verification.fail` | `record_verification` ← `verification/gate.py` | `passed`, `kept`, `dropped`, `flags` |
| `tool.success` / `tool.fail` | `record_tool_result` (helper in `observability.py`) | `tool`, `success` |

### Audit signals (structured logs, not Langfuse spans)

Emitted via structlog through `src/copilot/audit.py` and `openemr/roles.py`:
`copilot.audit.refusal`, `copilot.audit.break_glass`, `copilot.audit.role_denied`.
Surface these on the dashboard from the log pipeline (or a Langfuse event bridge
if added later); they are PHI-scrubbed at the log layer. They are *counts*, used
in the Governance row below.

---

## Dashboard layout

### Row 1 — Request health (top-line SLOs)

**Panel 1.1 · Request count**
- **Metric:** number of top-level requests served.
- **Source:** count of `patient_summary` spans (summary endpoint) +
  `answer_followup` spans (chat endpoint), grouped by span name.
- **Derivation:** `count(spans where name in ('patient_summary','answer_followup'))`
  over the selected window, split by name.

**Panel 1.2 · Error rate**
- **Metric:** fraction of requests that failed.
- **Source:** `patient_summary` and `answer_followup` span `success` metadata
  (and/or span `level == 'ERROR'`).
- **Derivation:**
  `count(spans where name in (…) and (metadata.success == false OR level == 'ERROR')) / count(spans where name in (…))`.
  Render as a percentage, split by span name.

**Panel 1.3 · Latency p50 / p95 / p99**
- **Metric:** request latency distribution.
- **Source:** `metadata.duration_ms` on the `patient_summary` span (primary SLO)
  and `answer_followup` span (chat SLO).
- **Derivation:** `percentile(metadata.duration_ms, [50,95,99])` grouped by span
  name over the window. p95 of `patient_summary.duration_ms` is the number the
  latency alert (see `alerts.yaml`) watches.

### Row 2 — Tool calls

**Panel 2.1 · Tool-call counts**
- **Metric:** calls per downstream tool span.
- **Source:** the tool/LLM spans in the table above (`get_patient`,
  `get_critical_set`, `get_deltas_since_last_visit`, `get_todays_schedule`,
  `is_patient_in_panel`, `break_glass`, `resolve_role`, `prewarm_schedule`,
  `llm.summarize`, `llm.followup`).
- **Derivation:** `count(spans) group by name`, stacked bar over time.

**Panel 2.2 · Tool-failure counts / rate**
- **Metric:** failed tool calls per tool, and failure rate.
- **Source:** the same tool spans' `metadata.success == false` /
  `level == 'ERROR'`.
- **Derivation:** per tool span `name`,
  `count(success == false) / count(*)`. The tool-failure alert watches the
  aggregate across all tool spans.

**Panel 2.3 · Tool retries**
- **Metric:** retry pressure on downstream calls.
- **Source:** downstream FHIR reads (`openemr/client.py`) and LLM calls
  (`llm/client.py`) retry transient failures with `tenacity`
  (`max_attempts = 3`, exponential backoff). Retries are internal to those
  clients, so the per-span observable signal is elevated `duration_ms` and
  eventual `success`/failure. The metadata allowlist in `observability.py`
  reserves the `retry_count` / `attempt` keys for when a caller attaches them.
- **Derivation:** if `metadata.retry_count` is present, `sum(retry_count) group by name`;
  otherwise proxy retry pressure as spans whose `duration_ms` exceeds the
  first-attempt-timeout band (a widened tail on Panel 1.3 for `get_*` / `llm.*`
  spans). Documented as a proxy, not an invented metric.

### Row 3 — Verification (grounding gate quality, FR-8/9/10)

**Panel 3.1 · Verification pass/fail rate**
- **Metric:** fraction of generated summaries that passed the grounding gate.
- **Source:** `verification.pass` / `verification.fail` events from
  `record_verification` (emitted in `verification/gate.py`).
- **Derivation:** `count(verification.pass) / (count(verification.pass) + count(verification.fail))`.

**Panel 3.2 · Claims kept vs dropped**
- **Metric:** grounding yield.
- **Source:** `kept`, `dropped`, `flags` metadata on the verification events and
  on the `verify` span.
- **Derivation:** `sum(metadata.dropped)` and `sum(metadata.kept)` over the
  window; also plot `sum(metadata.flags)` (deterministic rule hits).

### Row 4 — Governance (audit counts)

**Panel 4.1 · Refusals / break-glass / role-denials**
- **Metric:** counts of governance events.
- **Source:** structured logs `copilot.audit.refusal`,
  `copilot.audit.break_glass`, `copilot.audit.role_denied`
  (`src/copilot/audit.py`, `openemr/roles.py`).
- **Derivation:** count each event name over the window. Break-glass is the
  high-attention series (emitted at WARNING).

---

## Global dashboard conventions

- **Filter:** all panels filter by `correlation_id` when drilling into a single
  request trace.
- **PHI:** every value shown is already scrubbed by `scrub_phi()` — IDs, resource
  types, timestamps, and structural metadata only; clinical values are
  `[REDACTED]` at source and never reach Langfuse.
- **Default window:** 1h rolling, 15m buckets; alert windows are defined
  independently in `alerts.yaml`.
