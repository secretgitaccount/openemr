# PRP M3-5 · Observability dashboard + alerts

**Milestone:** M3 · **Depends on:** M0-6 observability · **Needs API key:** no

## Goal
The Langfuse dashboard + ≥3 alerts + on-call responses (PRD §13). Langfuse isn't running locally, so this is a **committed specification + config** deliverable (definitions + a runbook), grounded in the trace/event names the code actually emits.

## Context
- Reuse the real span/event names from `observability.py` and the code: spans `patient_summary`, `get_critical_set`, `get_deltas_since_last_visit`, `llm.summarize`, `llm.followup`, `resolve_role`, `answer_followup`; events `record_verification` (pass/fail), tool success/fail, `copilot.audit.{refusal,break_glass,role_denied}`. Confirm the exact names by grepping before writing (do not invent metrics that aren't emitted).
- Own new dir only: `observability/`.

## Spec
- `observability/DASHBOARD.md` — the dashboard spec: request count, error rate, p50/p95 latency (from `patient_summary` span duration), tool-call counts + retry counts (per tool span), verification pass/fail rate (from `record_verification`). For each panel: the metric, its source span/event, and the query/derivation.
- `observability/alerts.yaml` (or `.json`) — ≥3 alert definitions: (1) p95 latency > threshold, (2) error rate > threshold, (3) tool-failure rate > threshold — each with condition, threshold, window, and severity.
- `observability/RUNBOOK.md` — the documented **on-call response** per alert (what to check, likely causes tied to real spans, mitigation).

## Validation
```bash
cd agent && python -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('observability/*.y*ml')]; print('alerts parse')"
# and cross-check the referenced span/event names exist in the code:
grep -RoE 'trace\("[a-z_.]+"|record_verification|copilot\.audit\.[a-z_]+' src/copilot | sort -u | head
```
The alerts file parses; every metric/alert references a span or event that actually exists in `src/copilot` (no invented names).

## Definition of done
A dashboard spec, ≥3 parseable alert definitions, and a per-alert on-call runbook — all keyed to the telemetry the agent really emits — committed under `observability/`.
