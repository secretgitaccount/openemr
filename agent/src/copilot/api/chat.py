"""The multi-turn conversation endpoints — start a chart chat, ask follow-ups.

Two endpoints wire the M2 conversation lifecycle behind the same streamed-NDJSON
surface as the M1 summary endpoint:

* ``POST /patients/{patient_id}/conversation`` starts a conversation. It runs the
  full **role → panel → cache-through retrieve → summary** lifecycle, pins the
  patient, and streams the cited summary followed by a ``conversation`` event
  carrying the ``conversation_id`` the client uses for follow-ups. An out-of-panel
  or role-denied request streams a single refusal event and pins nothing.
* ``POST /conversations/{conversation_id}/messages`` answers a follow-up. The
  conversation resolves the pinned patient (pronouns like "she" never re-select a
  chart), the answer is grounded against the retained records, and each cited
  claim is streamed. An unknown/expired conversation returns ``404``.

Conversation and cache-through state lives in a **process-wide shared cache**
(:data:`SHARED_CACHE`) — the in-memory stand-in for the production Redis (NFR-5)
— so a follow-up resolves a conversation started by an earlier request even when
the two land on different orchestrator instances. The prewarm endpoint warms the
*same* cache, so a prewarmed chart opens warm here.

The network wiring (OAuth → user-bound token → FHIR client + role gate) lives
behind :func:`get_chat_orchestrator` so tests override it wholesale with an
orchestrator built over fakes — no key, no live stack.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from copilot.api.summary import (
    NDJSON_MEDIA_TYPE,
    _break_glass_reason,
    _line,
    _provider_id,
    _sources,
    stream_summary,
)
from copilot.config import get_settings
from copilot.logging import get_logger
from copilot.openemr.client import FhirClient
from copilot.openemr.oauth import TokenProvider, register_client
from copilot.orchestrator.cache import TTLCache
from copilot.orchestrator.controller import (
    FollowupResult,
    HandRolledOrchestrator,
    PatientSummary,
)
from copilot.orchestrator.conversation import ConversationNotFoundError

__all__ = [
    "router",
    "SHARED_CACHE",
    "get_chat_orchestrator",
    "stream_start",
    "stream_followup",
]

logger = get_logger(__name__)

router = APIRouter(tags=["conversation"])

#: Process-wide short-lived shared cache backing conversation state (M2-2) and
#: cache-through critical-set reads (M2-4). In production this is a Redis-backed
#: :class:`~copilot.orchestrator.cache.Cache`; the in-memory :class:`TTLCache`
#: here keeps the seam identical. It persists across requests (module scope) so a
#: follow-up resolves a conversation started earlier, and the prewarm endpoint
#: warms the very same instance.
SHARED_CACHE: TTLCache[Any] = TTLCache()


# ---------------------------------------------------------------------------
# Dependency — the network-wired, role-gated orchestrator (overridden in tests)
# ---------------------------------------------------------------------------


async def get_chat_orchestrator() -> AsyncIterator[HandRolledOrchestrator]:
    """Yield a fully-wired conversation orchestrator for one request.

    Registers (or reuses) the OAuth2 client, builds a user-bound
    :class:`TokenProvider` — which is also the role gate's token source (M2-3) —
    and opens a :class:`FhirClient` so every read happens as the clinician
    (FR-3). The orchestrator shares the process-wide :data:`SHARED_CACHE` so its
    conversation and cache-through state outlive the request. Tests override this
    dependency with an orchestrator built over fakes.
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
        yield HandRolledOrchestrator(
            fhir_client=client,
            token_source=provider,
            cache=SHARED_CACHE,
        )


# ---------------------------------------------------------------------------
# Request body + event serialisation
# ---------------------------------------------------------------------------


class FollowupRequest(BaseModel):
    """The body of a follow-up message: just the question to answer."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, description="The follow-up question to answer.")


def stream_start(result: PatientSummary, conversation_id: str | None) -> Iterator[str]:
    """Stream the started conversation: the cited summary, then a ``conversation``.

    Delegates the summary/refusal events to the M1 :func:`stream_summary` renderer
    so the citation contract is identical, then — when access was granted and a
    conversation was pinned — appends one ``conversation`` event carrying the id
    the client uses to ask follow-ups. A refused start pins nothing and appends no
    such line.
    """

    yield from stream_summary(result)
    if conversation_id is not None:
        yield _line(
            {
                "type": "conversation",
                "conversation_id": conversation_id,
                "patient_id": result.patient_id,
            }
        )


def stream_followup(result: FollowupResult) -> Iterator[str]:
    """Stream a follow-up answer: each grounded claim (cited), drops, then caveats.

    Every ``answer`` event carries its ``source_id`` pointers (FR-8) so the UI can
    link back to the record. A claim dropped by the grounding gate is surfaced as
    a ``dropped`` notice rather than shown as fact; ``caveat`` lines are the
    answer's plain-language hedges.
    """

    yield _line(
        {
            "type": "conversation",
            "conversation_id": result.conversation_id,
            "patient_id": result.patient_id,
        }
    )
    for claim in result.answer.answer:
        yield _line(
            {"type": "answer", "text": claim.text, "sources": _sources(claim.sources)}
        )
    for claim in result.dropped:
        yield _line({"type": "dropped", "text": claim.text})
    for caveat in result.answer.caveats:
        yield _line({"type": "caveat", "text": caveat})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/patients/{patient_id}/conversation")
async def start_conversation_endpoint(
    patient_id: str,
    x_break_glass_reason: str | None = Header(
        default=None,
        alias="X-Break-Glass-Reason",
        description=(
            "Break-glass justification. Locally, Synthea patients have no "
            "schedule, so this is how the granted path is reached; logged as an "
            "override. Leave blank for the normal gated path."
        ),
    ),
    x_provider_id: str | None = Header(
        default=None,
        alias="X-Provider-Id",
        description="Acting provider (dev seam; defaults to admin).",
    ),
    full_labs: bool = Query(
        default=False,
        description="Analyse the full lab history instead of the recent+abnormal slice (slower, costlier).",
    ),
    orchestrator: HandRolledOrchestrator = Depends(get_chat_orchestrator),
) -> StreamingResponse:
    """Start a chart conversation and stream the cited summary + ``conversation_id``.

    Runs the role → panel → retrieve → summary lifecycle; a granted request pins
    the patient and returns the id for follow-ups, a refused one streams a single
    refusal event and pins nothing.
    """

    provider_id = _provider_id(x_provider_id)
    break_glass_reason = _break_glass_reason(x_break_glass_reason)
    result, conversation_id = await orchestrator.start_conversation(
        patient_id, provider_id, break_glass_reason=break_glass_reason, full_labs=full_labs
    )

    return StreamingResponse(
        stream_start(result, conversation_id),
        media_type=NDJSON_MEDIA_TYPE,
    )


@router.post("/conversations/{conversation_id}/messages", response_model=None)
async def followup_endpoint(
    conversation_id: str,
    body: FollowupRequest,
    x_provider_id: str | None = Header(
        default=None,
        alias="X-Provider-Id",
        description="Acting provider (dev seam; defaults to admin).",
    ),
    orchestrator: HandRolledOrchestrator = Depends(get_chat_orchestrator),
) -> StreamingResponse | JSONResponse:
    """Answer a follow-up over a started conversation and stream the cited answer.

    The pinned patient is resolved from the conversation (never from the
    question), the answer is grounded against the retained records, and each cited
    claim is streamed as NDJSON. An unknown or expired conversation returns
    ``404`` rather than starting a fresh, unpinned chat.
    """

    provider_id = _provider_id(x_provider_id)
    try:
        result = await orchestrator.answer_followup(
            conversation_id, body.question, provider_id
        )
    except ConversationNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"detail": "No live conversation for that id (unknown or expired)."},
        )

    return StreamingResponse(
        stream_followup(result),
        media_type=NDJSON_MEDIA_TYPE,
    )
