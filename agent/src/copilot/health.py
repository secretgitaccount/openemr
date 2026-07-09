"""Liveness (`/health`) and readiness (`/ready`) endpoints (FR-15).

Liveness and readiness are deliberately **separate concerns**:

* **`GET /health`** — process liveness. Returns ``{"status": "ok"}`` with HTTP
  200 whenever the process is alive enough to answer. It performs no dependency
  I/O, so an orchestrator can use it to decide *restart-or-not* without being
  confused by a transient downstream outage.
* **`GET /ready`** — readiness to actually serve traffic. It genuinely probes
  each dependency and returns a per-dependency status plus an overall HTTP
  ``200`` (ready) / ``503`` (not ready). It never returns 200 unconditionally
  (the PRD engineering requirement) and it **fails loud**, naming the
  dependency that is down.

Dependencies checked (PRD FR-15):

* **OpenEMR API** — HTTP ``GET {base}/apis/default/fhir/metadata`` (the SMART
  capability statement). ``200``/``401`` both mean *reachable* (401 just means
  the server is up and demanding auth). Gates readiness.
* **Anthropic** — API key present and non-placeholder. A pure key-present check
  so readiness never spends tokens. Gates readiness (Claude is required to
  synthesize).
* **Langfuse** — reachable when configured; ``not_configured`` when keys are
  absent. Observability degrades gracefully, so this **never** gates readiness
  (a dev box with no Langfuse keys is still "ready").
* **OpenEMR audit globals** — a stubbed assertion hook. The real check (that
  ``api_log_option >= 1`` and ``enable_auditlog == 1``) reads OpenEMR globals
  and is wired in M1; today it reports ``skipped`` and does not gate readiness.

Every probe runs under a short timeout and can never hang ``/ready``: network
checks share one :class:`httpx.AsyncClient` with a bounded timeout, run
concurrently, and translate any exception into an ``unreachable`` status rather
than propagating.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from copilot.config import Settings, get_settings

__all__ = ["router", "DependencyStatus", "ReadinessReport", "HealthStatus"]

router = APIRouter(tags=["health"])

#: Per-dependency network timeout. Deliberately short so ``/ready`` stays snappy
#: and a hung dependency degrades to ``unreachable`` instead of stalling probes.
DEP_TIMEOUT_SECONDS: float = 2.0
#: The OpenEMR FHIR capability statement is heavy; probe it with more headroom.
OPENEMR_PROBE_TIMEOUT_SECONDS: float = 15.0


# ---------------------------------------------------------------------------
# Response contracts (NFR-3: Pydantic is the source of truth for API shape)
# ---------------------------------------------------------------------------


class HealthStatus(BaseModel):
    """Liveness payload — intentionally trivial."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"


class DependencyStatus(BaseModel):
    """Outcome of a single dependency probe.

    ``status`` is one of ``ok`` | ``unreachable`` | ``not_configured`` |
    ``degraded`` | ``skipped``. ``required`` marks whether this dependency
    gates overall readiness — a required dependency in any status other than
    ``ok`` forces ``/ready`` to ``503``.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(description="ok | unreachable | not_configured | degraded | skipped")
    required: bool = Field(default=False, description="Whether this gates readiness.")
    detail: str | None = Field(default=None, description="Human-readable probe detail.")
    latency_ms: float | None = Field(default=None, description="Probe round-trip, if applicable.")


class ReadinessReport(BaseModel):
    """Aggregate readiness across all dependencies."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(description="ready | not_ready")
    checks: dict[str, DependencyStatus]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_placeholder(value: str | None) -> bool:
    """True when a credential is empty or a scaffold placeholder (``…xxxx…``)."""

    return not value or "xxxx" in value.lower()


# ---------------------------------------------------------------------------
# Individual dependency probes
#
# Each returns a DependencyStatus and never raises: a probe failure is a
# *result*, not an error to propagate. They are module-level so tests can
# monkeypatch a single dependency to simulate an outage.
# ---------------------------------------------------------------------------


async def _check_openemr(client: httpx.AsyncClient, settings: Settings) -> DependencyStatus:
    """Probe the OpenEMR FHIR capability statement; 200/401 == reachable."""

    url = settings.openemr_base_url.rstrip("/") + "/apis/default/fhir/metadata"
    started = time.perf_counter()
    try:
        # The FHIR capability statement is a large, dynamically-built document
        # (~5s over a public round-trip), so it gets a longer timeout than the
        # other dependency probes — real data queries are far lighter.
        resp = await client.get(url, timeout=OPENEMR_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # httpx.ConnectError, TimeoutException, etc.
        return DependencyStatus(
            status="unreachable",
            required=True,
            detail=f"{type(exc).__name__}: could not reach {url}",
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    if resp.status_code in (200, 401):
        return DependencyStatus(
            status="ok",
            required=True,
            detail=f"HTTP {resp.status_code} from FHIR metadata",
            latency_ms=latency_ms,
        )
    return DependencyStatus(
        status="unreachable",
        required=True,
        detail=f"unexpected HTTP {resp.status_code} from FHIR metadata",
        latency_ms=latency_ms,
    )


async def _check_anthropic(settings: Settings) -> DependencyStatus:
    """Key-present check for Claude — never spends tokens (PRP constraint)."""

    if _is_placeholder(settings.anthropic_api_key):
        return DependencyStatus(
            status="not_configured",
            required=True,
            detail="ANTHROPIC_API_KEY missing or placeholder",
        )
    return DependencyStatus(
        status="ok",
        required=True,
        detail="API key present",
    )


async def _check_langfuse(client: httpx.AsyncClient, settings: Settings) -> DependencyStatus:
    """Probe Langfuse when configured; degrade (never fail) when it is not."""

    if _is_placeholder(settings.langfuse_public_key) or _is_placeholder(
        settings.langfuse_secret_key
    ):
        return DependencyStatus(
            status="not_configured",
            required=False,
            detail="Langfuse keys absent; observability disabled (dev)",
        )

    url = settings.langfuse_host.rstrip("/") + "/api/public/health"
    started = time.perf_counter()
    try:
        resp = await client.get(url)
    except Exception as exc:
        return DependencyStatus(
            status="degraded",
            required=False,
            detail=f"{type(exc).__name__}: could not reach Langfuse",
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    # Any non-5xx answer means the ingestion host is up and routing.
    if resp.status_code < 500:
        return DependencyStatus(
            status="ok",
            required=False,
            detail=f"HTTP {resp.status_code} from Langfuse health",
            latency_ms=latency_ms,
        )
    return DependencyStatus(
        status="degraded",
        required=False,
        detail=f"HTTP {resp.status_code} from Langfuse health",
        latency_ms=latency_ms,
    )


async def _check_audit_globals(settings: Settings) -> DependencyStatus:
    """Stub for the OpenEMR audit-globals assertion (wired for real in M1).

    The real check will assert OpenEMR's ``api_log_option >= 1`` and
    ``enable_auditlog == 1`` so every borrowed-identity read lands in the audit
    log (FR-14/FR-15). It is not gating today; it reports ``skipped``.
    """

    return DependencyStatus(
        status="skipped",
        required=False,
        detail="assert-on-startup TODO: api_log_option>=1, enable_auditlog=1 (wired in M1)",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Liveness: 200 whenever the process is alive. No dependency I/O."""

    return HealthStatus(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessReport,
    responses={503: {"model": ReadinessReport}},
)
async def ready() -> JSONResponse:
    """Readiness: probe every dependency; 200 only when all required ones pass."""

    settings = get_settings()

    async with httpx.AsyncClient(timeout=DEP_TIMEOUT_SECONDS) as client:
        openemr, langfuse, anthropic, audit = await asyncio.gather(
            _check_openemr(client, settings),
            _check_langfuse(client, settings),
            _check_anthropic(settings),
            _check_audit_globals(settings),
        )

    checks: dict[str, DependencyStatus] = {
        "openemr": openemr,
        "anthropic": anthropic,
        "langfuse": langfuse,
        "audit_globals": audit,
    }

    is_ready = all(c.status == "ok" for c in checks.values() if c.required)
    report = ReadinessReport(
        status="ready" if is_ready else "not_ready",
        checks=checks,
    )
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content=report.model_dump(),
    )
