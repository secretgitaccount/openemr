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

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from copilot.config import get_settings
from copilot.logging import get_logger
from copilot.openemr.client import FhirClient
from copilot.openemr.oauth import TokenProvider, register_client
from copilot.openemr.smart import StaticTokenSource
from copilot.smart_session import SESSION_COOKIE, get_session
from copilot.orchestrator.controller import (
    CaveatEvent,
    ClaimEvent,
    DataAsOfEvent,
    FlagEvent,
    HandRolledOrchestrator,
    HeadlineEvent,
    NoticeEvent,
    Orchestrator,
    PatientSummary,
    RefusalEvent,
    SummaryEvent,
)
from copilot.schemas.core import SourceRef

__all__ = [
    "router",
    "get_orchestrator",
    "stream_summary",
    "_provider_id",
    "_break_glass_reason",
]

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


async def get_orchestrator(request: Request) -> AsyncIterator[Orchestrator]:
    """Yield a fully-wired :class:`Orchestrator` for the life of one request.

    Identity is resolved per request:

    * If the caller carries a **SMART launch session** (the clinician launched
      the agent from OpenEMR), reads borrow *that clinician's* token
      (:class:`StaticTokenSource`) — the production-correct borrowed identity.
    * Otherwise the agent falls back to the dev **password grant** as ``admin``
      (the standalone demo path).

    Either way the same token source drives the FHIR reads *and* the role gate,
    so a non-clinical identity is refused before any clinical read (FR-3). Tests
    override this dependency with a fake orchestrator, so no OAuth or live stack
    is touched in unit tests.
    """

    settings = get_settings()
    token_source: object
    session = get_session(request.cookies.get(SESSION_COOKIE))
    if session is not None:
        token_source = StaticTokenSource(session.access_token)
    else:
        creds = register_client(settings=settings)
        token_source = TokenProvider(
            settings.openemr_dev_user,
            settings.openemr_dev_pass,
            settings=settings,
            credentials=creds,
        )
    async with FhirClient(token_source, settings=settings) as client:
        yield HandRolledOrchestrator(fhir_client=client, token_source=token_source)


# ---------------------------------------------------------------------------
# Provider resolution (auth context; dev = admin)
# ---------------------------------------------------------------------------


def _provider_id(value: str | None) -> str:
    """Resolve the acting provider from the ``X-Provider-Id`` header value.

    Real SMART/OAuth provider binding lands with authentication; for the dev
    flow the provider is ``admin`` unless ``X-Provider-Id`` overrides it (a
    convenience seam for smoke-testing a specific practitioner's panel).
    """

    return value.strip() if value and value.strip() else _DEV_PROVIDER_ID


def _break_glass_reason(value: str | None) -> str | None:
    """Resolve an explicit break-glass justification from ``X-Break-Glass-Reason``.

    Threads through to the orchestrator's break-glass path (M1-2 audit), making a
    paneled-by-override request reachable over HTTP — locally, where Synthea
    patients have no schedule, this is the only way the granted happy path is
    reachable. A missing or blank value means the normal gated path (no override).
    """

    return value.strip() if value and value.strip() else None


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

        # A large lab history was bounded to the recent+abnormal slice (NFR-4).
        # Surface the count so the UI can offer an opt-in full re-analysis.
        if result.labs_omitted > 0:
            yield _line({"type": "lab_overflow", "omitted": result.labs_omitted})

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
# Progressive stream serialisation (M3-2) — render each stage event as it lands
# ---------------------------------------------------------------------------


def _stream_event_line(event: SummaryEvent) -> str:
    """Render one orchestrator :class:`SummaryEvent` to a single NDJSON line.

    The wire shape is identical to :func:`stream_summary`'s (a refusal is one
    ``refusal`` line; every claim/flag carries its ``source_id`` pointers), so a
    client sees the same events whether the summary is streamed progressively or
    rendered from a completed envelope.
    """

    match event:
        case RefusalEvent():
            return _line(
                {"type": "refusal", "patient_id": event.patient_id, "reason": event.reason}
            )
        case HeadlineEvent():
            return _line({"type": "headline", "text": event.text})
        case ClaimEvent():
            return _line(
                {
                    "type": event.kind,
                    "text": event.claim.text,
                    "sources": _sources(event.claim.sources),
                }
            )
        case FlagEvent():
            return _line(
                {
                    "type": "flag",
                    "rule": event.flag.rule,
                    "severity": event.flag.severity,
                    "message": event.flag.message,
                    "sources": _sources(event.flag.sources),
                }
            )
        case CaveatEvent():
            return _line({"type": "caveat", "text": event.text})
        case NoticeEvent():
            return _line(
                {
                    "type": "notice",
                    "field": event.field,
                    "text": f"Could not retrieve {event.field}; showing what is available.",
                }
            )
        case DataAsOfEvent():
            return _line({"type": "data_as_of", "timestamp": event.timestamp.isoformat()})


async def _serialize_stream(events: AsyncIterator[SummaryEvent]) -> AsyncIterator[str]:
    """Render an orchestrator event stream to NDJSON lines as each event lands."""

    async for event in events:
        yield _stream_event_line(event)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/patients/{patient_id}/summary")
async def patient_summary_endpoint(
    patient_id: str,
    x_break_glass_reason: str | None = Header(
        default=None,
        alias="X-Break-Glass-Reason",
        description=(
            "Break-glass justification. Locally, Synthea patients have no "
            "schedule, so this is how the granted (in-panel) path is reached; "
            "the override is logged. Leave blank for the normal gated path."
        ),
    ),
    x_provider_id: str | None = Header(
        default=None,
        alias="X-Provider-Id",
        description="Acting provider (dev seam; defaults to admin).",
    ),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> StreamingResponse:
    """Stream a grounded, cited "what changed + must-knows" summary (M1 acceptance).

    Runs the gate-first lifecycle and streams the finalized pieces as NDJSON. An
    out-of-panel patient yields a single refusal event (the refusal is logged by
    the gate); a paneled patient yields the cited summary, safety flags, "couldn't
    retrieve X" notices, and a "data as of <ts>" line.
    """

    provider_id = _provider_id(x_provider_id)
    break_glass_reason = _break_glass_reason(x_break_glass_reason)

    # Prefer true progressive streaming (each stage emitted as it finalizes)
    # when the orchestrator supports it; fall back to compute-then-render for a
    # bare :class:`Orchestrator` (e.g. a test fake with only ``patient_summary``).
    streamer = getattr(orchestrator, "stream_patient_summary", None)
    if streamer is not None:
        return StreamingResponse(
            _serialize_stream(
                streamer(patient_id, provider_id, break_glass_reason=break_glass_reason)
            ),
            media_type=NDJSON_MEDIA_TYPE,
        )

    result = await orchestrator.patient_summary(
        patient_id, provider_id, break_glass_reason=break_glass_reason
    )
    return StreamingResponse(
        stream_summary(result),
        media_type=NDJSON_MEDIA_TYPE,
    )
