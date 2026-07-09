# Clinical Co-Pilot — On-Call Runbook (M3-5)

One section per alert in `alerts.yaml`. Each says what fired, what to check
first (keyed to the real spans/events in `src/copilot`), likely causes, and
mitigation. All trace payloads are PHI-scrubbed (`scrub_phi`), so you are
triaging on IDs, resource types, `success`, and `duration_ms` — never clinical
values.

General first move for any alert: open Langfuse, filter to the alert's window,
and pull a handful of failing traces by `correlation_id`. The correlation ID is
on every span (`observability.trace`) and every audit log line, so it stitches
the whole request path — API → orchestrator → tools → LLM → verify — together.

---

## summary_p95_latency_high

**Fired:** p95 of `patient_summary.metadata.duration_ms` > 8000 ms for 10m.

**Check first**
1. Which child span dominates the slow `patient_summary` traces? Compare
   `duration_ms` on the children: `get_critical_set`, `get_deltas_since_last_visit`,
   `get_patient`, `llm.summarize`, `verify`.
2. Is it the LLM leg (`llm.summarize`) or the FHIR retrieval legs
   (`get_critical_set` / `get_deltas_since_last_visit` / `get_patient`)?
3. Check tool-failure/retry pressure — `tenacity` retries (`max_attempts = 3`,
   exponential backoff) in `openemr/client.py` and `llm/client.py` inflate
   `duration_ms` without necessarily failing the span.

**Likely causes**
- Anthropic latency/throttling → slow `llm.summarize`.
- OpenEMR/FHIR backend slow or rate-limiting → slow `get_*` spans, elevated
  retries.
- Cold cache: `prewarm_schedule` not run, so `SHARED_CACHE` misses force full
  retrieval on the request path.

**Mitigation**
- If LLM-bound: confirm Anthropic status; the summary path degrades on LLM
  failure (`LLMError`) — verify graceful behavior is intact, consider lowering
  `max_attempts` backoff ceiling.
- If FHIR-bound: check OpenEMR health/latency; confirm OAuth token refresh isn't
  thrashing.
- If cache-bound: ensure the prewarm job is running so schedules are warm.
- If sustained and load-driven: scale replicas (see `deploy/`, M3-6).

---

## request_error_rate_high

**Fired:** `count(failure) / count(total) > 5%` across `patient_summary` +
`answer_followup` for 10m (failure = `metadata.success == false` or
`level == 'ERROR'`), min 20 samples.

**Check first**
1. Group failing top-level spans by `status_message` — this is the exception
   *type* (e.g. `LLMError`, an httpx/OAuth error type). No message is stored, by
   design.
2. Descend into the failing traces: which child span first flipped to
   `level == 'ERROR'`? That localizes the fault (retrieval vs LLM vs verify).
3. Cross-check governance logs: a burst of `copilot.audit.role_denied` or
   `copilot.audit.refusal` may be *expected* denials, not system errors — those
   are separate from span failures.

**Likely causes**
- Anthropic outage/auth failure → `llm.summarize` / `llm.followup` raising
  `LLMError`.
- OpenEMR OAuth token or FHIR endpoint failing → `get_*` spans error.
- Bad deploy / config regression (missing env, wrong base URL).

**Mitigation**
- Isolate the dominant `status_message` and treat as its own incident.
- If a recent deploy correlates, roll back (M3-6 `deploy/`).
- If upstream (Anthropic/OpenEMR) is down, confirm the app degrades safely and
  post status; do not retry-storm.

---

## tool_failure_rate_high

**Fired:** `count(failure) / count(total) > 10%` across the downstream tool/LLM
spans for 5m, min 20 samples.

**Check first**
1. Break the failure rate down **per span name** — is it one tool
   (`get_critical_set`, `get_deltas_since_last_visit`, `get_patient`,
   `get_todays_schedule`, `is_patient_in_panel`, `break_glass`, `resolve_role`)
   or the LLM spans (`llm.summarize`, `llm.followup`)?
2. Read `status_message` (exception type) on the failing spans.
3. If a single FHIR tool dominates, the OpenEMR resource/endpoint it reads is
   the suspect; if `resolve_role` dominates, the role/permission lookup path is.

**Likely causes**
- OpenEMR FHIR endpoint down / 5xx / rate-limited (exhausts `tenacity`
  `max_attempts`).
- OAuth token expiry/refresh failure hitting all FHIR tools at once.
- Anthropic throttling for the `llm.*` spans.

**Mitigation**
- Single-tool failure → check that specific OpenEMR resource endpoint.
- All FHIR tools failing together → treat as an auth/token incident.
- LLM-only → Anthropic status; confirm summary path degrades gracefully.

---

## verification_fail_rate_high

**Fired:** `verification.fail / (pass + fail) > 20%` for 15m, min 10 samples.
Source: `record_verification` events from `verification/gate.py`.

**Check first**
1. On `verification.fail` events / the `verify` span, read `dropped`, `kept`,
   `flags`. A high `dropped` count means claims are being generated whose source
   refs are not present in the `critical_set` (ungrounded claims).
2. Correlate with `get_critical_set` health — if retrieval is returning thin or
   partial sets, more claims will fail grounding.
3. Check whether `llm.summarize` output shape changed (model/prompt drift) —
   compare against the eval suite (M3-3, `tests/eval/`).

**Likely causes**
- Retrieval regression: `get_critical_set` / `get_deltas_since_last_visit`
  returning incomplete data, so valid-ref set shrinks.
- LLM drift: model producing claims with unsupported/hallucinated source refs.
- Prompt or schema change that weakened grounding.

**Mitigation**
- If retrieval-driven: fix the upstream data path; the gate is working correctly
  by dropping ungrounded claims (fail-closed is the intended safety behavior).
- If LLM-driven: run the eval suite; if regression confirmed, roll back
  model/prompt.
- The gate dropping claims is *safe* (no ungrounded PHI reaches the user) — do
  not disable it to clear the alert.

---

## break_glass_spike

**Fired:** `count(copilot.audit.break_glass) > 10` in 1h. Source: structured
audit log from `openemr/panel.py` / `audit.py` (emitted at WARNING).

**Check first**
1. Pull the `copilot.audit.break_glass` log lines for the window; group by
   `provider_id`. Metadata is scrubbed — you have provider/patient **IDs**,
   timestamps, and a redacted reason, which is enough for access-review triage.
2. Is the spike one provider (possible misuse / training gap) or many (possible
   panel-membership data problem forcing legitimate break-glass)?
3. Cross-check `is_patient_in_panel` span failures — if panel resolution is
   broken, providers may be break-glassing around a bug rather than a real
   out-of-panel access.

**Likely causes**
- Legitimate surge (e.g. covering clinician, cross-coverage shift).
- Panel-membership data/resolution bug forcing unnecessary break-glass.
- Anomalous access pattern requiring security/compliance review.

**Mitigation**
- If tied to `is_patient_in_panel` failures: fix panel resolution; that removes
  the forced break-glass.
- If access looks anomalous: escalate to security/compliance per policy with the
  scrubbed audit trail (IDs + timestamps + correlation IDs). Break-glass access
  is allowed by design but always audited — this alert exists to guarantee a
  human reviews spikes.
