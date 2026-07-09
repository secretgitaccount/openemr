"""The streamed, cited patient-summary endpoint — the M1 acceptance surface.

``POST /patients/{patient_id}/summary`` runs the full request lifecycle through
the :class:`~copilot.orchestrator.controller.Orchestrator` and streams the
result back as newline-delimited JSON (``application/x-ndjson``): the headline,
each must-know and what-changed **claim rendered with its ``source_id``(s)**, the
deterministic safety flags, the FR-12 "couldn't retrieve X" notices, and a
closing "data as of <ts>" line. An out-of-panel patient streams a single refusal
event and nothing else — no chart data ever leaves the gate.

The network wiring (OAuth registration → user-bound token → FHIR client) lives
behind the :func:`get_orchestrator` dependency so it can be overridden wholesale
in tests with a fake orchestrator — the streaming/citation logic is exercised
without a key or a live stack.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from copilot.config import get_settings
from copilot.logging import get_logger
from copilot.openemr.client import FhirClient
from copilot.openemr.oauth import TokenProvider, register_client
from copilot.orchestrator.controller import (
    HandRolledOrchestrator,
    Orchestrator,
    PatientSummary,
)
from copilot.schemas.core import SourceRef

__all__ = ["router", "get_orchestrator", "stream_summary"]

logger = get_logger(__name__)

router = APIRouter(tags=["summary"])

#: Media type for the newline-delimited JSON event stream.
NDJSON_MEDIA_TYPE = "application/x-ndjson"

#: Dev provider identity. Real auth (the logged-in clinician) is wired later;
#: until then every request runs in the ``admin`` provider context (PRD dev).
_DEV_PROVIDER_ID = "admin"


# ---------------------------------------------------------------------------
# Dependency — the network-wired orchestrator (overridden in tests)
# ---------------------------------------------------------------------------


async def get_orchestrator() -> AsyncIterator[Orchestrator]:
    """Yield a fully-wired :class:`Orchestrator` for the life of one request.

    Registers (or reuses) the OAuth2 client, builds a user-bound
    :class:`TokenProvider`, and opens a :class:`FhirClient` so every read happens
    as the clinician (FR-3). The client is closed when the request finishes.
    Tests override this dependency with a fake orchestrator, so no OAuth or live
    stack is touched in unit tests.
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
        yield HandRolledOrchestrator(fhir_client=client)


# ---------------------------------------------------------------------------
# Provider resolution (auth context; dev = admin)
# ---------------------------------------------------------------------------


def _provider_id(request: Request) -> str:
    """Resolve the acting provider from the request auth context.

    Real SMART/OAuth provider binding lands with authentication; for the dev
    flow the provider is ``admin`` unless an ``X-Provider-Id`` header overrides
    it (a convenience seam for smoke-testing a specific practitioner's panel).
    """

    header = request.headers.get("X-Provider-Id")
    return header.strip() if header and header.strip() else _DEV_PROVIDER_ID


# ---------------------------------------------------------------------------
# Event serialisation (each rendered claim links to a source_id)
# ---------------------------------------------------------------------------


def _sources(sources: list[SourceRef]) -> list[dict[str, str | None]]:
    """Render grounding sources as citable ``source_id`` pointers (FR-8)."""

    return [
        {
            "source_id": f"{s.resource_type}/{s.id}",
            "resource_type": s.resource_type,
            "id": s.id,
            "timestamp": s.timestamp.isoformat() if s.timestamp else None,
        }
        for s in sources
    ]


def _line(event: dict[str, object]) -> str:
    """Serialise one event as a single NDJSON line."""

    return json.dumps(event, separators=(",", ":")) + "\n"


def stream_summary(result: PatientSummary) -> Iterator[str]:
    """Yield the NDJSON event stream for a completed :class:`PatientSummary`.

    A refusal streams exactly one ``refusal`` event. Otherwise the stream is:
    ``headline`` → each ``must_know`` / ``what_changed`` claim (with its
    ``sources``) → any deterministic ``flag`` → any caveat → one ``notice`` per
    un-retrieved tier (FR-12) → a closing ``data_as_of`` line. Every claim and
    flag carries its ``source_id`` pointers so the UI can link back to the record.
    """

    if result.refused:
        yield _line(
            {
                "type": "refusal",
                "patient_id": result.patient_id,
                "reason": result.decision.reason,
            }
        )
        return

    verified = result.verified
    if verified is not None:
        summary = verified.summary
        yield _line({"type": "headline", "text": summary.headline})

        for claim in summary.must_knows:
            yield _line(
                {"type": "must_know", "text": claim.text, "sources": _sources(claim.sources)}
            )
        for claim in summary.whats_changed:
            yield _line(
                {"type": "what_changed", "text": claim.text, "sources": _sources(claim.sources)}
            )
        for flag in verified.flags:
            yield _line(
                {
                    "type": "flag",
                    "rule": flag.rule,
                    "severity": flag.severity,
                    "message": flag.message,
                    "sources": _sources(flag.sources),
                }
            )
        for caveat in summary.caveats:
            yield _line({"type": "caveat", "text": caveat})

    # FR-12: name what could not be retrieved as an explicit notice — "couldn't
    # retrieve X", never a silent gap. When retrieval succeeded but the model
    # step failed, the summary itself is the missing piece.
    for name in result.missing:
        yield _line(
            {
                "type": "notice",
                "field": name,
                "text": f"Could not retrieve {name}; showing what is available.",
            }
        )

    if result.data_as_of is not None:
        yield _line({"type": "data_as_of", "timestamp": result.data_as_of.isoformat()})


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/patients/{patient_id}/summary")
async def patient_summary_endpoint(
    patient_id: str,
    request: Request,
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> StreamingResponse:
    """Stream a grounded, cited "what changed + must-knows" summary (M1 acceptance).

    Runs the gate-first lifecycle and streams the finalized pieces as NDJSON. An
    out-of-panel patient yields a single refusal event (the refusal is logged by
    the gate); a paneled patient yields the cited summary, safety flags, "couldn't
    retrieve X" notices, and a "data as of <ts>" line.
    """

    provider_id = _provider_id(request)
    result = await orchestrator.patient_summary(patient_id, provider_id)

    return StreamingResponse(
        stream_summary(result),
        media_type=NDJSON_MEDIA_TYPE,
    )
