"""Locust load profile for the Clinical Co-Pilot agent (PRP M3-4, PRD §13).

Exercises the public HTTP surface under concurrency to record p50/p95/p99
latency and error rate at 10 and 50 users:

* ``GET /health``  — liveness (cheap; high weight).
* ``GET /ready``   — readiness (probes dependencies).
* ``POST /patients/{id}/summary`` — the full retrieve → gate → verify → stream
  lifecycle, streamed as NDJSON. The break-glass header makes the granted happy
  path reachable locally (Synthea patients have no schedule, so the normal panel
  gate would refuse). This is the expensive task and the one that matters.

**Zero Claude spend.** Run the agent under ``COPILOT_LLM_STUB=1`` so
``LLMClient`` returns a canned, source-bound summary instead of calling
Anthropic (see ``src/copilot/llm/client.py``). This measures the app's own
throughput — routing, gating, verification, streaming — without token cost or
network to ``api.anthropic.com``. ``run.sh`` sets the flag for you.

Configuration via environment variables:

* ``COPILOT_HOST``          — base URL of the running agent (default
  ``http://localhost:8000``; Locust's ``--host`` overrides this).
* ``COPILOT_PATIENT_IDS``   — comma-separated FHIR Patient ids to summarise
  (default ``1``). Provide a handful of real local ids for a realistic mix.
* ``COPILOT_BREAK_GLASS_REASON`` — justification sent on every summary request
  (default a load-test marker).

Run headless via ``run.sh`` (10 then 50 users) or interactively with
``locust -f loadtest/locustfile.py``.
"""

from __future__ import annotations

import os

from locust import HttpUser, between, task


def _patient_ids() -> list[str]:
    raw = os.getenv("COPILOT_PATIENT_IDS", "1")
    ids = [p.strip() for p in raw.split(",") if p.strip()]
    return ids or ["1"]


PATIENT_IDS: list[str] = _patient_ids()
BREAK_GLASS_REASON: str = os.getenv(
    "COPILOT_BREAK_GLASS_REASON", "load-test: synthetic throughput measurement"
)


class CopilotUser(HttpUser):
    """A synthetic clinician hammering the agent's HTTP surface.

    ``wait_time`` inserts 0.5–2s of think time between tasks so the offered load
    resembles clinicians paging through charts rather than a tight benchmark
    loop. Task weights make ``/health`` the most frequent (cheap poller) and the
    summary the second-most (the flow under test).
    """

    #: Default host; overridden by ``--host`` / ``COPILOT_HOST``.
    host = os.getenv("COPILOT_HOST", "http://localhost:8000")

    wait_time = between(0.5, 2.0)

    @task(5)
    def health(self) -> None:
        self.client.get("/health", name="GET /health")

    @task(1)
    def ready(self) -> None:
        # /ready returns 503 when a dependency is down; treat that as a
        # non-failing, expected outcome so it doesn't pollute the error rate.
        with self.client.get(
            "/ready", name="GET /ready", catch_response=True
        ) as resp:
            if resp.status_code in (200, 503):
                resp.success()
            else:
                resp.failure(f"unexpected status {resp.status_code}")

    @task(3)
    def patient_summary(self) -> None:
        # Round-robin over the configured patient ids for a realistic mix.
        patient_id = PATIENT_IDS[self._summary_index % len(PATIENT_IDS)]
        self._summary_index += 1
        headers = {"X-Break-Glass-Reason": BREAK_GLASS_REASON}
        with self.client.post(
            f"/patients/{patient_id}/summary",
            name="POST /patients/{id}/summary",
            headers=headers,
            stream=True,
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"unexpected status {resp.status_code}")
                return
            # Drain the NDJSON stream so latency reflects the full response, not
            # just the first byte.
            body = resp.content
            if not body:
                resp.failure("empty summary stream")
            else:
                resp.success()

    _summary_index: int = 0
