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

import time
from collections.abc import Callable

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from copilot.graph.answer import (
    AnswerDiagnostics,
    AnswerSynthesizer,
    W2Answer,
    build_answer,
)
from copilot.graph.state import Attachment, GraphInput, GraphResult
from copilot.graph.supervisor import run_graph
from copilot.logging import get_logger
from copilot.observability import (
    EncounterMetrics,
    StepLatency,
    WorkerLatency,
    estimate_cost_usd,
    record_encounter_metrics,
)

__all__ = [
    "router",
    "AskRequest",
    "GraphRunner",
    "get_graph_runner",
    "get_answer_synthesizer",
    "build_encounter_metrics",
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


def _mean_extraction_confidence(extracted: list[object]) -> float | None:
    """Mean VLM extraction confidence across the run's extracted documents.

    Unwraps the PRP-06 ``IngestResult`` (``.extracted``) as well as a bare
    ``LabReport`` / ``IntakeFacts`` (the chart-read path), reading each model's
    ``extraction_confidence``. ``None`` when nothing was extracted (a
    question-only run) so the metric distinguishes "no extraction" from "0.0".
    """

    confidences: list[float] = []
    for item in extracted:
        model = getattr(item, "extracted", item)
        conf = getattr(model, "extraction_confidence", None)
        if isinstance(conf, (int, float)):
            confidences.append(float(conf))
    return sum(confidences) / len(confidences) if confidences else None


def _retrieval_hit_rate(answer: W2Answer) -> float | None:
    """Fraction of retrieved guideline chunks a surviving claim actually cited.

    A per-encounter "retrieval hits" signal (FR-9): of the guideline evidence the
    retriever surfaced, how much was load-bearing for the grounded answer. ``None``
    when no guideline evidence was retrieved (nothing to rate).
    """

    retrieved = {c.source_id for c in answer.guideline_evidence}
    if not retrieved:
        return None
    cited = {
        s.id
        for claim in answer.answer_claims
        for s in claim.sources
        if s.id in retrieved
    }
    return len(cited) / len(retrieved)


def build_encounter_metrics(
    result: GraphResult,
    answer: W2Answer,
    diagnostics: AnswerDiagnostics,
    *,
    graph_latency_ms: float,
    answer_latency_ms: float,
    total_latency_ms: float,
) -> EncounterMetrics:
    """Assemble the PHI-free per-encounter metrics for one ``/ask`` run (FR-9).

    Every one of the seven required signals is populated here from data already
    on the graph result, the assembled answer, and the assembly diagnostics — no
    clinical value is read, only structural names / counts / latencies / tokens:

    * **tool sequence** — the worker nodes the supervisor routed to, in order;
    * **latency by step** — graph run vs answer assembly, plus per-worker timing;
    * **token usage / cost** — from the synthesis call (0 / ``None`` on a stub);
    * **retrieval hits** — fraction of retrieved guideline chunks actually cited;
    * **extraction confidence** — mean VLM confidence across extracted documents;
    * **eval outcome** — the online grounding verdict (``fail`` only when the
      model asserted claims and the gate dropped *all* of them as ungrounded).
    """

    tool_sequence = [h.to_node for h in result.handoffs if h.to_node != "done"]
    routing_decisions = [f"{h.from_node}->{h.to_node}" for h in result.handoffs]
    worker_latencies = [
        WorkerLatency(worker=w.worker, latency_ms=w.latency_ms, success=w.success)
        for w in result.worker_latencies
    ]

    tokens = diagnostics.input_tokens + diagnostics.output_tokens
    cost_usd = (
        estimate_cost_usd(diagnostics.model, diagnostics.input_tokens, diagnostics.output_tokens)
        if tokens
        else None
    )

    claims = len(answer.answer_claims)
    dropped = diagnostics.dropped_claims
    eval_outcome = "fail" if (claims == 0 and dropped > 0) else "pass"

    return EncounterMetrics(
        correlation_id=result.correlation_id,
        patient_id=result.patient_id,
        tool_sequence=tool_sequence,
        step_latencies=[
            StepLatency(step="graph_run", latency_ms=graph_latency_ms),
            StepLatency(step="answer_build", latency_ms=answer_latency_ms),
        ],
        total_latency_ms=total_latency_ms,
        worker_latencies=worker_latencies,
        routing_decisions=routing_decisions,
        steps=result.steps,
        model=diagnostics.model,
        input_tokens=diagnostics.input_tokens,
        output_tokens=diagnostics.output_tokens,
        cost_usd=cost_usd,
        retrieval_hit_rate=_retrieval_hit_rate(answer),
        extraction_confidence=_mean_extraction_confidence(result.extracted),
        claims=claims,
        dropped_claims=dropped,
        eval_outcome=eval_outcome,
    )


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

    started = time.monotonic()
    result = await run_in_threadpool(run, graph_input)
    graph_done = time.monotonic()

    diagnostics = AnswerDiagnostics()
    answer = await build_answer(result, synthesize=synthesize, diagnostics=diagnostics)
    finished = time.monotonic()

    metrics = build_encounter_metrics(
        result,
        answer,
        diagnostics,
        graph_latency_ms=(graph_done - started) * 1000.0,
        answer_latency_ms=(finished - graph_done) * 1000.0,
        total_latency_ms=(finished - started) * 1000.0,
    )
    # Emit as a Langfuse event (nested under the run's correlation id) AND as a
    # structured, PHI-free log line so the full per-encounter record is searchable
    # by correlation id even when observability is disabled.
    record_encounter_metrics(metrics)
    logger.info(
        "w2flow.ask.metrics",
        correlation_id=metrics.correlation_id,
        patient_id=patient_id,
        tool_sequence=metrics.tool_sequence,
        steps=metrics.steps,
        total_latency_ms=round(metrics.total_latency_ms, 1),
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        cost_usd=metrics.cost_usd,
        retrieval_hit_rate=metrics.retrieval_hit_rate,
        extraction_confidence=metrics.extraction_confidence,
        claims=metrics.claims,
        dropped_claims=metrics.dropped_claims,
        eval_outcome=metrics.eval_outcome,
    )
    return answer
