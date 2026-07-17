"""Worker nodes wrapping PRP-06 extraction and PRP-08 retrieval (PRP-09).

Two workers sit under the supervisor:

* **intake_extractor** wraps :func:`copilot.documents.ingest.attach_and_extract`
  (PRP-06): it turns each attached document into validated, cited facts.
* **evidence_retriever** wraps :func:`copilot.rag.retrieve.retrieve_evidence`
  (PRP-08): it pulls the top grounded guideline evidence for the question.

Each worker is expressed as a small *worker function* ``(GraphState) -> list``
(``default_intake_extractor`` / ``default_evidence_retriever``) that is
**injectable** — :func:`copilot.graph.supervisor.run_graph` accepts stubs so the
graph can be tested with no live key and no network. The ``make_*_node`` wrappers
add the observability span and translate the produced list into a LangGraph state
delta; the span is opened while the supervisor span is the current observation,
so worker spans nest under it (NFR-2).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from copilot.documents.chart_read import extract_chart_document
from copilot.documents.ingest import attach_and_extract
from copilot.observability import trace
from copilot.rag.retrieve import retrieve_evidence

from .state import GraphState, Handoff, WorkerTiming

__all__ = [
    "WorkerFn",
    "default_intake_extractor",
    "default_evidence_retriever",
    "make_intake_node",
    "make_evidence_node",
]

#: A worker function: reads the current state and returns the list it produced
#: (extracted facts, or evidence). Deliberately pure of tracing / state-delta
#: concerns so tests can inject a trivial stub.
WorkerFn = Callable[[GraphState], list[Any]]


def default_intake_extractor(state: GraphState) -> list[Any]:
    """Extract every attachment, choosing the path the ``Attachment`` names.

    * ``document_id`` set → the PRP-15/17 chart-read path
      (:func:`copilot.documents.chart_read.extract_chart_document`): it grounds
      ``/ask`` on a document **already in the chart** and is **cache-first** — if
      the doctor already read that document the extraction is reused (no second
      VLM call) and, either way, nothing is re-persisted.
    * ``file_path`` set → the PRP-06 upload path
      (:func:`copilot.documents.ingest.attach_and_extract`).

    Runs the async tools synchronously (the graph is invoked outside any event
    loop). Returns one extracted result per attachment. Injected out in tests —
    no VLM/key is touched there.
    """

    patient_id = state["patient_id"]
    results: list[Any] = []
    for attachment in state["attachments"]:
        if attachment.document_id is not None:
            results.append(
                asyncio.run(
                    extract_chart_document(
                        patient_id, attachment.document_id, attachment.doc_type
                    )
                )
            )
        else:
            results.append(
                asyncio.run(
                    attach_and_extract(
                        patient_id, attachment.file_path, attachment.doc_type
                    )
                )
            )
    return results


def default_evidence_retriever(state: GraphState) -> list[Any]:
    """Retrieve grounded guideline evidence for the question via PRP-08."""

    return list(retrieve_evidence(state["question"]))


def _make_worker_node(
    node_name: str,
    worker: WorkerFn,
    *,
    output_key: str,
    span_input: Callable[[GraphState], dict[str, Any]],
) -> Callable[[GraphState], dict[str, Any]]:
    """Build a traced graph node that runs ``worker`` and survives its failures.

    On success the produced list is written to ``output_key`` and control returns
    to the supervisor. If the worker raises, the node **does not propagate** —
    that would destroy the inspectable handoff log (FR-5) and let a raw,
    possibly-PHI-bearing exception message escape the scrubber. Instead it appends
    a terminal ERROR :class:`Handoff` whose ``reason`` names only the exception
    **type** (never its message) and flips ``done`` so the supervisor terminates
    with the routing record intact.

    Either way it appends a :class:`WorkerTiming` (monotonic wall-clock + success
    flag) to the ``worker_latencies`` channel, so per-worker latency (FR-9) is
    reconstructable from state alone even when the worker failed.
    """

    def node(state: GraphState) -> dict[str, Any]:
        started = time.monotonic()
        try:
            with trace(
                f"graph.{node_name}",
                as_type="span",
                metadata={
                    "node": node_name,
                    "correlation_id": state["correlation_id"],
                },
                input=span_input(state),
            ) as span:
                produced = list(worker(state))
                span.update(output={f"{output_key}_count": len(produced)})
        except Exception as exc:
            # PHI-safe: record the exception *type* only — the message may carry
            # clinical values (e.g. "K 5.9 mmol/L") that must never be surfaced.
            handoff = Handoff(
                from_node=node_name,
                to_node="done",
                reason=(
                    f"{node_name} failed with {type(exc).__name__}; terminating "
                    "with the handoff log intact (details suppressed to keep PHI out)"
                ),
                at=datetime.now(UTC),
            )
            timing = WorkerTiming(
                worker=node_name,
                latency_ms=(time.monotonic() - started) * 1000.0,
                success=False,
            )
            return {"handoffs": [handoff], "worker_latencies": [timing], "done": True}
        timing = WorkerTiming(
            worker=node_name,
            latency_ms=(time.monotonic() - started) * 1000.0,
            success=True,
        )
        return {output_key: produced, "worker_latencies": [timing]}

    return node


def make_intake_node(worker: WorkerFn) -> Callable[[GraphState], dict[str, Any]]:
    """Wrap a worker fn as the ``intake_extractor`` graph node (traced)."""

    return _make_worker_node(
        "intake_extractor",
        worker,
        output_key="extracted",
        span_input=lambda state: {"attachments": len(state["attachments"])},
    )


def make_evidence_node(worker: WorkerFn) -> Callable[[GraphState], dict[str, Any]]:
    """Wrap a worker fn as the ``evidence_retriever`` graph node (traced)."""

    return _make_worker_node(
        "evidence_retriever",
        worker,
        output_key="evidence",
        span_input=lambda state: {"question": state["question"]},
    )
