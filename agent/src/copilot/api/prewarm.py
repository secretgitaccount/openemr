"""The schedule-prewarm endpoint — warm the day's charts into the shared cache.

``POST /prewarm`` fetches the acting provider's schedule for today and warms each
patient's :class:`~copilot.schemas.clinical.CriticalSet` into the process-wide
:data:`~copilot.api.chat.SHARED_CACHE` (M2-4, FR-13), so opening one of those
charts later reads a warm cache instead of waiting on the tiered FHIR retrieval.
It returns the :class:`~copilot.orchestrator.prewarm.PrewarmReport` — counts and
timings only, no clinical values — so an operator can see what was warmed.

Two invariants come straight from the prewarm module: it is **data-only** (no LLM
call ever happens here — Claude cost stays linear in visits opened, not schedule
size) and **never fatal** (a single chart's retrieval failing is recorded in the
report, not raised).

The FHIR wiring lives behind :func:`get_prewarm_client` so tests override it with
a fake client and stub the schedule/retrieval at the prewarm-module boundary — no
key, no live stack.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header

from copilot.api.chat import SHARED_CACHE
from copilot.api.summary import _provider_id
from copilot.config import get_settings
from copilot.logging import get_logger
from copilot.openemr.client import FhirClient
from copilot.openemr.oauth import TokenProvider, register_client
from copilot.orchestrator.prewarm import PrewarmReport, prewarm_schedule

__all__ = ["router", "get_prewarm_client"]

logger = get_logger(__name__)

router = APIRouter(tags=["prewarm"])


async def get_prewarm_client() -> AsyncIterator[FhirClient]:
    """Yield a user-bound :class:`FhirClient` for the life of one prewarm request.

    Registers (or reuses) the OAuth2 client, builds a user-bound
    :class:`TokenProvider`, and opens a :class:`FhirClient` so every schedule and
    chart read happens as the clinician (FR-3). Tests override this dependency
    with a fake client.
    """

    settings = get_settings()
    creds = register_client(settings=settings)
    provider = TokenProvider(
        settings.openemr_dev_user,
        settings.openemr_dev_pass,
        settings=settings,
        credentials=creds,
    )
    async with FhirClient(provider, settings=settings) as client:
        yield client


@router.post("/prewarm")
async def prewarm_endpoint(
    x_provider_id: str | None = Header(
        default=None,
        alias="X-Provider-Id",
        description="Provider whose schedule to prewarm (dev seam; defaults to admin).",
    ),
    client: FhirClient = Depends(get_prewarm_client),
) -> PrewarmReport:
    """Warm today's scheduled charts into the shared cache and return the report.

    Runs :func:`prewarm_schedule` for the acting provider against the same
    process-wide cache the chart/conversation endpoints read through, so warmed
    charts open warm. Data-only and never fatal (per-patient failures are recorded
    in the returned :class:`PrewarmReport`, not raised).
    """

    provider_id = _provider_id(x_provider_id)
    report = await prewarm_schedule(provider_id, client=client, cache=SHARED_CACHE)
    logger.info(
        "copilot.prewarm.completed",
        provider_id=provider_id,
        scheduled=report.scheduled,
        warmed=report.warmed,
        failed=len(report.failed),
    )
    return report
