"""The Week-2 flow endpoint: ask a grounded question about a patient (PRP-10).

``POST /patients/{patient_id}/ask`` is the Week-2 acceptance surface. It runs the
PRP-09 supervisor graph over the question (extracting any attached documents and
retrieving guideline evidence as the supervisor decides), then assembles the
verified :class:`~copilot.graph.answer.W2Answer`: record facts kept distinct from
guideline evidence, every surfaced claim grounded through the Week-1 verification
gate, plus the inspectable handoff log.

Two seams keep this key- and network-free in tests, mirroring the summary/chat
routers' override pattern:

* :func:`get_graph_runner` yields the graph runner; tests override it with one
  wired to **stub workers** (no VLM, no RAG models).
* :func:`get_answer_synthesizer` yields the LLM synthesis step; tests override it
  with a **stub** so no Anthropic call is made.

The graph runner is synchronous (and its default workers call ``asyncio.run``),
so it is driven off the event loop via :func:`run_in_threadpool`; the async
synthesis + gate then run on the loop.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from copilot.graph.answer import AnswerSynthesizer, W2Answer, build_answer
from copilot.graph.state import Attachment, GraphInput, GraphResult
from copilot.graph.supervisor import run_graph
from copilot.logging import get_logger

__all__ = [
    "router",
    "AskRequest",
    "GraphRunner",
    "get_graph_runner",
    "get_answer_synthesizer",
]

logger = get_logger(__name__)

router = APIRouter(tags=["w2flow"])

#: The graph runner's call signature: (GraphInput) -> GraphResult. Behind a
#: dependency so tests substitute a runner wired to stub workers (no key).
GraphRunner = Callable[[GraphInput], GraphResult]


class AskRequest(BaseModel):
    """The Week-2 ``/ask`` request body.

    ``attachments`` are documents to extract before answering (may be empty); the
    supervisor only routes to extraction when at least one is present. Frozen +
    ``extra="forbid"`` so a malformed body is a 422, not a silent drop.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, description="The clinical question to answer.")
    attachments: list[Attachment] = Field(
        default_factory=list,
        description="Documents to extract before answering (may be empty).",
    )


def get_graph_runner() -> GraphRunner:
    """Yield the supervisor graph runner (overridden in tests with stub workers)."""

    return run_graph


def get_answer_synthesizer() -> AnswerSynthesizer | None:
    """Yield the answer synthesis step (``None`` = default LLM path; stubbed in tests)."""

    return None


@router.post("/patients/{patient_id}/ask")
async def ask_endpoint(
    patient_id: str,
    body: AskRequest,
    run: GraphRunner = Depends(get_graph_runner),
    synthesize: AnswerSynthesizer | None = Depends(get_answer_synthesizer),
) -> W2Answer:
    """Answer one grounded question about a patient and return the verified answer.

    Runs the PRP-09 graph (off the event loop) then assembles the
    :class:`W2Answer`: record facts and guideline evidence kept as distinct
    fields, every surfaced claim verified/grounded (ungrounded ones dropped), and
    the routing handoff log returned so the whole run is replayable.
    """

    graph_input = GraphInput(
        patient_id=patient_id,
        question=body.question,
        attachments=list(body.attachments),
    )
    result = await run_in_threadpool(run, graph_input)
    answer = await build_answer(result, synthesize=synthesize)
    logger.info(
        "w2flow.ask.ok",
        patient_id=patient_id,
        steps=result.steps,
        claims=len(answer.answer_claims),
    )
    return answer
