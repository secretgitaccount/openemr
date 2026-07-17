"""The inspectable supervisor: routes to workers and terminates (PRP-09, FR-5).

One supervisor node makes every routing decision and terminates the graph;
:func:`run_graph` is the public entry point.

Routing (each decision appends a :class:`~copilot.graph.state.Handoff` carrying a
human-readable reason, so the whole route is replayable from state):

    needs extraction  — a document is attached and not yet extracted → intake_extractor
    needs evidence    — the question needs guideline support and none retrieved → evidence_retriever
    both satisfied    → done

**Termination is guaranteed two ways.** Normally the supervisor stops once both
needs are met. A **max-steps guard** is the backstop: if a worker never satisfies
its need (e.g. a degraded retriever returning nothing), the supervisor would loop
forever, so once ``max_steps`` routing decisions have been logged it force-routes
to ``done``. The number of decisions is just ``len(handoffs)`` — the append-only
handoff log doubles as the step counter.

**Observability nesting.** ``run_graph`` opens a single ``graph.supervisor`` span
around the whole ``graph.invoke``; each worker opens its own span *inside* that
invoke, so — because :func:`copilot.observability.trace` uses the current
observation as parent — worker spans nest under the supervisor span, all tagged
with the run's ``correlation_id`` (NFR-2).
"""

from __future__ import annotations

from datetime import UTC, datetime

from langgraph.graph import END, START, StateGraph

from copilot.logging import (
    current_correlation_id,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from copilot.observability import trace

from .state import GraphInput, GraphResult, GraphState, Handoff
from .workers import (
    WorkerFn,
    default_evidence_retriever,
    default_intake_extractor,
    make_evidence_node,
    make_intake_node,
)

__all__ = ["MAX_STEPS", "build_graph", "run_graph"]

#: Backstop cap on routing decisions before the supervisor force-terminates.
#: Generous relative to the two real needs (extraction + evidence) so it only
#: ever trips on a genuinely stuck worker, never on a healthy run.
MAX_STEPS = 12


def _needs_extraction(state: GraphState) -> bool:
    """A document is attached/referenced and has not yet been extracted."""

    return bool(state["attachments"]) and not state["extracted"]


def _needs_evidence(state: GraphState) -> bool:
    """The question needs guideline support and none has been retrieved."""

    return bool(state["question"] and state["question"].strip()) and not state["evidence"]


def _decide(state: GraphState, max_steps: int) -> tuple[str, str]:
    """Return the ``(target, reason)`` for the next route from ``state``.

    The max-steps guard is checked first so a stuck worker can never outrun it.
    """

    decisions = len(state["handoffs"])
    if decisions >= max_steps:
        return (
            "done",
            f"max-steps guard tripped after {decisions} routing decisions; "
            "forcing termination to avoid an infinite loop",
        )
    if _needs_extraction(state):
        return (
            "intake_extractor",
            "a document is attached and not yet extracted -> intake_extractor",
        )
    if _needs_evidence(state):
        return (
            "evidence_retriever",
            "question needs guideline support and none retrieved -> evidence_retriever",
        )
    return ("done", "extraction and evidence needs satisfied -> done")


def _make_supervisor(max_steps: int):
    """Build the supervisor node bound to ``max_steps``."""

    def supervisor(state: GraphState) -> dict:
        target, reason = _decide(state, max_steps)
        handoff = Handoff(
            from_node="supervisor",
            to_node=target,
            reason=reason,
            at=datetime.now(UTC),
        )
        delta: dict = {"handoffs": [handoff]}
        if target == "done":
            delta["done"] = True
        return delta

    return supervisor


def _route(state: GraphState) -> str:
    """Conditional-edge selector: follow the supervisor's last logged handoff."""

    return state["handoffs"][-1].to_node


def _after_worker(state: GraphState) -> str:
    """Return to the supervisor, unless a worker failed and terminated the run.

    A worker that raises appends a terminal ERROR handoff and sets ``done`` (see
    :func:`copilot.graph.workers._make_worker_node`); that must go straight to
    END so the failure is surfaced with the handoff log intact rather than
    looping the supervisor back into the same failing worker.
    """

    return "done" if state["done"] else "supervisor"


def build_graph(
    intake_extractor: WorkerFn = default_intake_extractor,
    evidence_retriever: WorkerFn = default_evidence_retriever,
    *,
    max_steps: int = MAX_STEPS,
):
    """Compile the supervisor/worker graph with the given (injectable) workers.

    ``intake_extractor`` / ``evidence_retriever`` are worker functions
    (``(GraphState) -> list``); tests pass stubs so the graph runs with no key.
    """

    graph = StateGraph(GraphState)
    graph.add_node("supervisor", _make_supervisor(max_steps))
    graph.add_node("intake_extractor", make_intake_node(intake_extractor))
    graph.add_node("evidence_retriever", make_evidence_node(evidence_retriever))

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route,
        {
            "intake_extractor": "intake_extractor",
            "evidence_retriever": "evidence_retriever",
            "done": END,
        },
    )
    for worker in ("intake_extractor", "evidence_retriever"):
        graph.add_conditional_edges(
            worker,
            _after_worker,
            {"supervisor": "supervisor", "done": END},
        )
    return graph.compile()


def run_graph(
    request: GraphInput,
    *,
    intake_extractor: WorkerFn = default_intake_extractor,
    evidence_retriever: WorkerFn = default_evidence_retriever,
    max_steps: int = MAX_STEPS,
) -> GraphResult:
    """Run the supervisor graph for ``request`` and return an inspectable result.

    Binds a correlation ID for the whole run (reusing the active one, else the
    request's, else a fresh uuid), opens the single ``graph.supervisor`` span
    that worker spans nest under, and drives the graph to termination. The
    returned :class:`GraphResult` carries the full ``handoffs`` routing log.
    """

    correlation_id = (
        request.correlation_id or current_correlation_id() or new_correlation_id()
    )
    token = None
    if current_correlation_id() != correlation_id:
        token = set_correlation_id(correlation_id)

    try:
        graph = build_graph(intake_extractor, evidence_retriever, max_steps=max_steps)
        initial: GraphState = {
            "correlation_id": correlation_id,
            "patient_id": request.patient_id,
            "question": request.question,
            "attachments": list(request.attachments),
            "extracted": [],
            "evidence": [],
            "handoffs": [],
            "worker_latencies": [],
            "done": False,
        }

        with trace(
            "graph.supervisor",
            as_type="span",
            metadata={"node": "supervisor", "correlation_id": correlation_id},
            input={
                "question": request.question,
                "attachments": len(request.attachments),
            },
        ) as span:
            # recursion_limit is a hard LangGraph backstop *behind* our own
            # max-steps guard: two super-steps (supervisor + worker) per decision,
            # plus slack, so our guard always trips first with a logged reason.
            final: GraphState = graph.invoke(
                initial, config={"recursion_limit": 2 * max_steps + 4}
            )
            span.update(
                output={
                    "handoffs": len(final["handoffs"]),
                    "done": final["done"],
                }
            )

        return GraphResult(
            correlation_id=correlation_id,
            patient_id=final["patient_id"],
            question=final["question"],
            extracted=list(final["extracted"]),
            evidence=list(final["evidence"]),
            handoffs=list(final["handoffs"]),
            worker_latencies=list(final.get("worker_latencies", [])),
            done=final["done"],
            steps=len(final["handoffs"]),
        )
    finally:
        if token is not None:
            reset_correlation_id(token)
