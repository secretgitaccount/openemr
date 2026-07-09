"""Background, data-only prewarm of the day's scheduled patients (FR-13, NFR-5).

Click-time latency is dominated by the tiered FHIR retrieval that assembles a
patient's :class:`~copilot.schemas.clinical.CriticalSet`. This module warms that
data ahead of time: for every patient on the provider's schedule today it fetches
the critical set concurrently (bounded) and stores it in the short-lived shared
:class:`~copilot.orchestrator.cache.Cache` under a stable key, so opening a chart
reads a warm cache instead of waiting on four FHIR round-trips.

Two hard rules keep the design honest:

* **Data-only.** Prewarm never calls the LLM. Claude synthesis stays at
  click-time, so Claude cost is linear in *visits opened*, not schedule size
  (FR-13). This module imports no LLM client.
* **Never fatal.** A single patient's retrieval failing is recorded in
  :attr:`PrewarmReport.failed`, not raised — the batch always completes so one
  bad chart cannot starve the rest of the schedule.

The request path reads through :func:`cached_critical_set`: a cache hit returns
the prewarmed set with no FHIR call; a miss fetches live and populates the cache
so the next reader is warm too. The orchestrator adopts this in M2-5.
"""

from __future__ import annotations

import asyncio
import time

from pydantic import BaseModel, ConfigDict, Field

from copilot.observability import trace
from copilot.openemr.client import FhirClient
from copilot.openemr.panel import get_todays_schedule
from copilot.openemr.retrieval import get_critical_set
from copilot.orchestrator.cache import Cache
from copilot.schemas.clinical import CriticalSet

__all__ = [
    "PrewarmReport",
    "prewarm_schedule",
    "cached_critical_set",
    "critical_set_key",
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_CONCURRENCY",
]

#: Prewarmed data is short-lived: long enough to cover a clinic session's chart
#: opens, short enough that a stale chart is re-fetched rather than trusted. The
#: request path re-populates on miss, so a conservative TTL is safe.
DEFAULT_TTL_SECONDS: float = 15 * 60

#: Bound on concurrent per-patient retrievals so a large schedule does not open
#: an unbounded fan-out of FHIR connections.
DEFAULT_CONCURRENCY: int = 4

#: Cache-key prefix so prewarmed critical sets don't collide with other cache
#: users (e.g. conversation state).
_KEY_PREFIX = "critical_set:"


def critical_set_key(patient_id: str) -> str:
    """Stable cache key under which a patient's :class:`CriticalSet` is stored."""

    return f"{_KEY_PREFIX}{patient_id}"


class PrewarmReport(BaseModel):
    """Outcome of one :func:`prewarm_schedule` run (counts and timings only).

    Carries no clinical values — safe to log and trace. ``failed`` names the
    patient ids whose retrieval could not be warmed (recorded, never fatal).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheduled: int = Field(
        default=0,
        ge=0,
        description="Number of patients on today's schedule.",
    )
    warmed: int = Field(
        default=0,
        ge=0,
        description="Number of patients whose critical set was warmed into the cache.",
    )
    failed: list[str] = Field(
        default_factory=list,
        description="Patient ids whose retrieval failed (recorded, not fatal).",
    )
    elapsed_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock duration of the prewarm run, in seconds.",
    )


async def _warm_one(
    patient_id: str,
    *,
    client: FhirClient,
    cache: Cache[CriticalSet],
    semaphore: asyncio.Semaphore,
    ttl: float,
) -> str | None:
    """Warm one patient's critical set into the cache; return its id on failure.

    Returns ``None`` on success and ``patient_id`` when retrieval raised, so the
    caller can fold the failure into :attr:`PrewarmReport.failed` without letting
    it sink the batch.
    """

    async with semaphore:
        try:
            critical_set = await get_critical_set(patient_id, client=client)
        except Exception:
            # A single chart's retrieval failing is recorded, never fatal: the
            # request path will fetch it live on demand.
            return patient_id
        await cache.set(critical_set_key(patient_id), critical_set, ttl)
        return None


async def prewarm_schedule(
    provider_id: str,
    *,
    client: FhirClient,
    cache: Cache[CriticalSet],
    concurrency: int = DEFAULT_CONCURRENCY,
    ttl: float = DEFAULT_TTL_SECONDS,
) -> PrewarmReport:
    """Prewarm every scheduled patient's critical set into the shared cache.

    Fetches today's schedule for ``provider_id``, then fans the per-patient
    :func:`~copilot.openemr.retrieval.get_critical_set` retrievals out with a
    bounded ``concurrency``, storing each under :func:`critical_set_key` with a
    short ``ttl``. Data-only: no LLM call happens here (FR-13). Per-patient
    failures are recorded in the returned :class:`PrewarmReport`, never raised —
    the batch always completes. The wrapping span records counts and timings
    only (NFR-4).
    """

    with trace("prewarm_schedule", metadata={"provider_id": provider_id}) as span:
        started = time.perf_counter()

        schedule = await get_todays_schedule(provider_id, client=client)
        # De-duplicate: a patient with two appointments today is warmed once.
        patient_ids = list(dict.fromkeys(p.patient_id for p in schedule.data))

        semaphore = asyncio.Semaphore(max(1, concurrency))
        results = await asyncio.gather(
            *(
                _warm_one(
                    patient_id,
                    client=client,
                    cache=cache,
                    semaphore=semaphore,
                    ttl=ttl,
                )
                for patient_id in patient_ids
            )
        )

        failed = [pid for pid in results if pid is not None]
        report = PrewarmReport(
            scheduled=len(patient_ids),
            warmed=len(patient_ids) - len(failed),
            failed=failed,
            elapsed_s=round(time.perf_counter() - started, 4),
        )
        span.update(
            metadata={
                "scheduled": report.scheduled,
                "warmed": report.warmed,
                "failed_count": len(report.failed),
                "elapsed_s": report.elapsed_s,
            }
        )
        return report


async def cached_critical_set(
    patient_id: str,
    *,
    client: FhirClient,
    cache: Cache[CriticalSet],
    ttl: float = DEFAULT_TTL_SECONDS,
) -> CriticalSet:
    """Cache-through read of a patient's critical set for the request path.

    Returns the prewarmed set on a cache hit (no FHIR call); on a miss it fetches
    live via :func:`~copilot.openemr.retrieval.get_critical_set`, populates the
    cache under :func:`critical_set_key`, and returns it — so a cold chart warms
    itself for the next reader. Never calls the LLM.
    """

    key = critical_set_key(patient_id)
    cached = await cache.get(key)
    if cached is not None:
        return cached

    critical_set = await get_critical_set(patient_id, client=client)
    await cache.set(key, critical_set, ttl)
    return critical_set
