"""Tests for the LangGraph supervisor + 2 workers (PRP-09).

Workers are **stubbed** — no live key, no VLM, no network. These tests pin the
FR-5 contract: doc-bearing questions route intake_extractor -> evidence_retriever
-> done; evidence-only questions skip extraction; every routing decision is
logged as an inspectable Handoff with a human-readable reason; the correlation ID
is present on every node/span; worker spans nest under the supervisor span; and
the graph always terminates (a max-steps guard prevents infinite loops).
"""

from __future__ import annotations

import pytest

from copilot import observability
from copilot.graph.state import Attachment, GraphInput
from copilot.graph.supervisor import run_graph


# ---------------------------------------------------------------------------
# Stub workers (no key, no network)
# ---------------------------------------------------------------------------


def _stub_intake(state):
    """Pretend extraction produced one fact per attachment."""

    return [{"extracted_from": att.file_path} for att in state["attachments"]]


def _stub_evidence(state):
    """Pretend retrieval surfaced two guideline chunks."""

    return [
        {"chunk_id": "g1", "question": state["question"]},
        {"chunk_id": "g2", "question": state["question"]},
    ]


def _never_satisfies(state):
    """A degraded worker that returns nothing — forces the max-steps guard."""

    return []


def _doc_input() -> GraphInput:
    return GraphInput(
        patient_id="p-1",
        question="Is the potassium level dangerous?",
        attachments=[Attachment(file_path="/tmp/lab.pdf", doc_type="lab_pdf")],
        correlation_id="corr-doc-1",
    )


def _evidence_only_input() -> GraphInput:
    return GraphInput(
        patient_id="p-2",
        question="What is the target HbA1c for a type 2 diabetic?",
        correlation_id="corr-ev-1",
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_doc_question_routes_extractor_then_retriever_then_done() -> None:
    result = run_graph(
        _doc_input(),
        intake_extractor=_stub_intake,
        evidence_retriever=_stub_evidence,
    )

    assert result.done is True
    # extractor ran, then retriever ran
    assert len(result.extracted) == 1
    assert len(result.evidence) == 2

    # Routing visited extractor, then retriever, then terminated.
    route = [h.to_node for h in result.handoffs]
    assert route == ["intake_extractor", "evidence_retriever", "done"]
    assert result.steps == 3


def test_evidence_only_question_skips_extraction() -> None:
    result = run_graph(
        _evidence_only_input(),
        intake_extractor=_stub_intake,
        evidence_retriever=_stub_evidence,
    )

    assert result.done is True
    assert result.extracted == []  # extraction skipped entirely
    assert len(result.evidence) == 2

    route = [h.to_node for h in result.handoffs]
    assert "intake_extractor" not in route
    assert route == ["evidence_retriever", "done"]


# ---------------------------------------------------------------------------
# Handoff log records each decision + reason
# ---------------------------------------------------------------------------


def test_handoff_log_records_each_decision_with_reason() -> None:
    result = run_graph(
        _doc_input(),
        intake_extractor=_stub_intake,
        evidence_retriever=_stub_evidence,
    )

    reasons = [h.reason for h in result.handoffs]
    assert "not yet extracted" in reasons[0]
    assert "intake_extractor" in reasons[0]
    assert "guideline support" in reasons[1]
    assert "evidence_retriever" in reasons[1]
    assert "satisfied" in reasons[2]

    # Every handoff is a full, timestamped, from->to record.
    for h in result.handoffs:
        assert h.from_node == "supervisor"
        assert h.reason
        assert h.at is not None


# ---------------------------------------------------------------------------
# Termination + max-steps guard
# ---------------------------------------------------------------------------


def test_graph_terminates_with_healthy_workers() -> None:
    result = run_graph(
        _doc_input(),
        intake_extractor=_stub_intake,
        evidence_retriever=_stub_evidence,
    )
    assert result.done is True
    assert result.handoffs[-1].to_node == "done"


def _exploding_evidence(state):
    """A worker that raises mid-run (degraded retriever)."""

    raise ValueError("boom in worker: K 5.9 mmol/L")


def test_worker_exception_does_not_lose_handoff_log() -> None:
    # QA demonstrator (PRP-09 probe 2c): when a worker raises, the graph must
    # surface a handled failure that PRESERVES the inspectable handoff log, not
    # let a raw exception propagate out of run_graph and drop the routing record.
    try:
        result = run_graph(
            _evidence_only_input(),
            intake_extractor=_stub_intake,
            evidence_retriever=_exploding_evidence,
        )
    except Exception as exc:  # pragma: no cover - this is the defect being shown
        pytest.fail(
            "run_graph let a worker exception escape unhandled "
            f"({type(exc).__name__}); the handoff log is lost. Expected a "
            "handled failure that preserves handoffs."
        )
    # If it ever returns, the handoff log must still be inspectable.
    assert result.handoffs, "handoff log must survive a worker failure"


def test_max_steps_guard_prevents_infinite_loop() -> None:
    # evidence_retriever never satisfies the need -> without a guard the
    # supervisor would route to it forever. The guard must force termination.
    result = run_graph(
        _evidence_only_input(),
        intake_extractor=_stub_intake,
        evidence_retriever=_never_satisfies,
        max_steps=3,
    )

    assert result.done is True
    # Bounded: at most max_steps productive attempts + 1 guard decision.
    assert result.steps == 4
    assert result.handoffs[-1].to_node == "done"
    assert "max-steps guard" in result.handoffs[-1].reason


# ---------------------------------------------------------------------------
# Observability: correlation ID on every span + nesting under supervisor
# ---------------------------------------------------------------------------


class _FakeSpan:
    """Records the metadata it was opened with and its parent span."""

    def __init__(self, name: str, parent: _FakeSpan | None, metadata: dict) -> None:
        self.name = name
        self.parent = parent
        self.metadata = metadata or {}
        self.updates: list[dict] = []

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    def create_event(self, **kwargs) -> None:  # pragma: no cover - unused here
        pass


class _FakeObservation:
    """Context manager that pushes/pops the client's span stack on enter/exit."""

    def __init__(self, client: _FakeClient, name: str, metadata: dict) -> None:
        self._client = client
        self._name = name
        self._metadata = metadata
        self.span: _FakeSpan | None = None

    def __enter__(self) -> _FakeSpan:
        parent = self._client.stack[-1] if self._client.stack else None
        self.span = _FakeSpan(self._name, parent, self._metadata)
        self._client.spans.append(self.span)
        self._client.stack.append(self.span)
        return self.span

    def __exit__(self, *exc) -> bool:
        self._client.stack.pop()
        return False


class _FakeClient:
    """Minimal Langfuse stand-in that reconstructs span nesting from with-blocks."""

    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []
        self.stack: list[_FakeSpan] = []

    def start_as_current_observation(
        self, *, name: str, as_type: str = "span", input=None, metadata=None
    ) -> _FakeObservation:
        return _FakeObservation(self, name, metadata or {})

    def create_event(self, **kwargs) -> None:  # pragma: no cover - unused here
        pass

    def flush(self) -> None:  # pragma: no cover - unused here
        pass


@pytest.fixture
def _fake_langfuse(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr(observability, "get_langfuse_client", lambda: client)
    return client


def test_worker_spans_nest_under_supervisor_with_correlation_id(
    _fake_langfuse: _FakeClient,
) -> None:
    run_graph(
        _doc_input(),
        intake_extractor=_stub_intake,
        evidence_retriever=_stub_evidence,
    )

    spans = _fake_langfuse.spans
    by_name = {s.name: s for s in spans}
    assert "graph.supervisor" in by_name
    assert "graph.intake_extractor" in by_name
    assert "graph.evidence_retriever" in by_name

    supervisor = by_name["graph.supervisor"]
    # The supervisor span is the root of this trace.
    assert supervisor.parent is None
    # Both worker spans nest directly under the supervisor span.
    assert by_name["graph.intake_extractor"].parent is supervisor
    assert by_name["graph.evidence_retriever"].parent is supervisor

    # correlation_id is present on every emitted span.
    for span in spans:
        assert span.metadata.get("correlation_id") == "corr-doc-1"
