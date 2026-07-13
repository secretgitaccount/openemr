"""Liveness (`/health`) and readiness (`/ready`) endpoints (FR-15).

Liveness and readiness are deliberately **separate concerns**:

* **`GET /health`** — process liveness. Returns ``{"status": "ok"}`` with HTTP
  200 whenever the process is alive enough to answer. It performs no dependency
  I/O, so an orchestrator can use it to decide *restart-or-not* without being
  confused by a transient downstream outage.
* **`GET /ready`** — readiness to actually serve traffic. It genuinely probes
  each dependency and returns a per-dependency status plus an overall status
  that is **not a binary up/down**: ``ready`` (HTTP 200), ``degraded`` (HTTP
  200 — serving, but a non-gating dependency is down and is named), or
  ``not_ready`` (HTTP 503 — a required dependency is down). It never returns 200
  unconditionally (the PRD engineering requirement) and it **fails loud**,
  naming every dependency that is down.

Dependencies checked (PRD FR-15 + Week-2 FR-9):

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

Week-2 dependencies (PRP-14) — each is **non-gating** but a down one degrades
readiness (so ``/ready`` names it without 503-ing a still-serving agent):

* **Document storage** — the local OpenEMR Standard REST API (the document
  *write* path) is reachable: ``GET {base}/apis/default/api/facility``.
  ``200``/``401``/``403`` all mean *reachable* (the API is mounted and enforcing
  auth). Its own timeouts guard the probe.
* **Vector index** — the RAG guideline corpus loads and, when the hybrid index
  has already been built this process, it is reported *loaded/warm*. Purely
  local; never triggers a model download on the probe.
* **Reranker** — the cross-encoder rerank model is *loadable* (its library
  imports and the model name is configured), reported *warm* once loaded. The
  probe never forces the (~90 MB) download.

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
    """Aggregate readiness across all dependencies.

    ``status`` is three-valued, not binary: ``ready`` (all good), ``degraded``
    (serving, but a non-gating dependency is down — see ``degraded``), or
    ``not_ready`` (a required dependency is down → HTTP 503).
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(description="ready | degraded | not_ready")
    checks: dict[str, DependencyStatus]
    degraded: list[str] = Field(
        default_factory=list,
        description="Names of down (non-gating) dependencies; empty when fully ready.",
    )


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
# Week-2 dependency probes (PRP-14) — non-gating; a down one degrades readiness
# ---------------------------------------------------------------------------


async def _check_document_storage(
    client: httpx.AsyncClient, settings: Settings
) -> DependencyStatus:
    """Probe the OpenEMR Standard REST API (the document *write* path).

    ``GET {base}/apis/default/api/facility``; ``200``/``401``/``403`` all mean
    the API is mounted and reachable (401/403 just demand auth). ``404`` means
    the Standard API is not mounted (degraded); a transport error is
    ``unreachable``. Non-gating: ingestion needs it, but a Q&A over already-known
    records can still be served, so it never forces a 503.
    """

    url = settings.openemr_base_url.rstrip("/") + "/apis/default/api/facility"
    started = time.perf_counter()
    try:
        resp = await client.get(url, timeout=DEP_TIMEOUT_SECONDS)
    except Exception as exc:  # httpx.ConnectError, TimeoutException, etc.
        return DependencyStatus(
            status="unreachable",
            required=False,
            detail=f"{type(exc).__name__}: could not reach the Standard REST API at {url}",
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    if resp.status_code in (200, 401, 403):
        return DependencyStatus(
            status="ok",
            required=False,
            detail=f"HTTP {resp.status_code} from Standard REST API (write path reachable)",
            latency_ms=latency_ms,
        )
    return DependencyStatus(
        status="degraded",
        required=False,
        detail=f"unexpected HTTP {resp.status_code} from Standard REST API",
        latency_ms=latency_ms,
    )


async def _check_vector_index(settings: Settings) -> DependencyStatus:
    """Confirm the RAG guideline corpus loads and report whether the index is warm.

    Local only — reads the PRP-07 corpus and inspects the process-wide index
    cache. It never builds the index or downloads the embedding model on the
    probe path (that would block ``/ready``); a warm index just means a prior
    request already built it. A corpus that will not load is ``degraded``.
    """

    started = time.perf_counter()
    try:
        from copilot.rag.chunk import load_corpus
        from copilot.rag.index import _INDEX_CACHE

        corpus = load_corpus()
        n_chunks = len(list(corpus))
        warm = len(_INDEX_CACHE) > 0
    except Exception as exc:
        return DependencyStatus(
            status="degraded",
            required=False,
            detail=f"{type(exc).__name__}: guideline corpus / vector index unavailable",
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    if n_chunks <= 0:
        return DependencyStatus(
            status="degraded",
            required=False,
            detail="guideline corpus is empty; vector index cannot be built",
            latency_ms=latency_ms,
        )
    state = "loaded (index warm)" if warm else "loadable (index not yet built)"
    return DependencyStatus(
        status="ok",
        required=False,
        detail=f"guideline corpus present ({n_chunks} chunks); {state}",
        latency_ms=latency_ms,
    )


async def _check_reranker(settings: Settings) -> DependencyStatus:
    """Confirm the cross-encoder reranker is loadable, reporting warm-vs-lazy.

    Verifies ``sentence-transformers`` imports and the model name is configured;
    it does **not** force the (~90 MB) weight download on the probe. If the model
    singleton has already been loaded this process, it reports *warm*. A missing
    library / model name is ``degraded`` (retrieval reranking would fail).
    """

    started = time.perf_counter()
    try:
        import importlib.util

        from copilot.rag import retrieve as _retrieve

        model_name = _retrieve.RERANKER_MODEL_NAME
        if not model_name:
            raise RuntimeError("reranker model name is not configured")
        if importlib.util.find_spec("sentence_transformers") is None:
            raise RuntimeError("sentence-transformers is not installed")
        warm = _retrieve._reranker is not None
    except Exception as exc:
        return DependencyStatus(
            status="degraded",
            required=False,
            detail=f"{type(exc).__name__}: reranker model is not loadable",
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    state = "loaded (warm)" if warm else "loadable (lazy; not yet warmed)"
    return DependencyStatus(
        status="ok",
        required=False,
        detail=f"reranker '{model_name}' {state}",
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Liveness: 200 whenever the process is alive. No dependency I/O."""

    return HealthStatus(status="ok")


#: Per-dependency statuses that count as "down" for the degraded aggregation.
#: ``not_configured`` / ``skipped`` are deliberately excluded: a dev box with no
#: Langfuse keys, or the stubbed audit-globals hook, is not a degradation.
_DOWN_STATUSES: frozenset[str] = frozenset({"unreachable", "degraded"})


@router.get(
    "/ready",
    response_model=ReadinessReport,
    responses={503: {"model": ReadinessReport}},
)
async def ready() -> JSONResponse:
    """Readiness: probe every dependency and return a three-valued status.

    ``503 not_ready`` if any **required** dependency is down; otherwise ``200``
    with ``degraded`` (naming any down Week-2 dependency) or ``ready``. Never a
    binary up/down — a still-serving agent with a down non-gating dependency is
    reported as degraded, not failed.
    """

    settings = get_settings()

    async with httpx.AsyncClient(timeout=DEP_TIMEOUT_SECONDS) as client:
        (
            openemr,
            langfuse,
            anthropic,
            audit,
            document_storage,
            vector_index,
            reranker,
        ) = await asyncio.gather(
            _check_openemr(client, settings),
            _check_langfuse(client, settings),
            _check_anthropic(settings),
            _check_audit_globals(settings),
            _check_document_storage(client, settings),
            _check_vector_index(settings),
            _check_reranker(settings),
        )

    checks: dict[str, DependencyStatus] = {
        "openemr": openemr,
        "anthropic": anthropic,
        "langfuse": langfuse,
        "audit_globals": audit,
        "document_storage": document_storage,
        "vector_index": vector_index,
        "reranker": reranker,
    }

    required_ok = all(c.status == "ok" for c in checks.values() if c.required)
    down = [name for name, c in checks.items() if c.status in _DOWN_STATUSES]

    if not required_ok:
        status = "not_ready"
    elif down:
        status = "degraded"
    else:
        status = "ready"

    report = ReadinessReport(status=status, checks=checks, degraded=down)
    # Degraded still serves traffic (200); only a down *required* dep is a 503.
    return JSONResponse(
        status_code=503 if status == "not_ready" else 200,
        content=report.model_dump(),
    )
