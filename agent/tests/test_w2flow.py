"""HTTP-surface tests for the Week-2 ``/ask`` endpoint (PRP-10).

``POST /patients/{patient_id}/ask`` is driven through FastAPI's ``TestClient``
against the **real** graph runner, but with the two workers **stubbed** (no VLM,
no RAG models) and the LLM synthesis step **stubbed** (no Anthropic call) via the
router's dependency overrides — the same override seam the summary/documents
routers use. No key and no live stack are touched.

Coverage (the PRP's validation gates):

* record facts and guideline evidence come back as **distinct** JSON fields;
* an **ungrounded** claim is dropped by the gate (absent from ``answer_claims``);
* every surfaced claim carries a citation resolvable to the returned evidence;
* the inspectable **handoff log** is returned in the response.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from copilot.api.w2flow import (
    build_encounter_metrics,
    get_answer_synthesizer,
    get_graph_runner,
)
from copilot.documents.ingest import IngestResult
from copilot.documents.schemas import LabObservation, LabReport, SourceCitation
from copilot.graph.answer import AnswerDiagnostics, build_answer
from copilot.graph.state import Attachment, GraphInput
from copilot.graph.supervisor import run_graph
from copilot.main import app
from copilot.rag.chunk import GuidelineChunk
from copilot.rag.retrieve import GuidelineEvidence
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

LAB_DOC_ID = "lab_cmp_lipid.pdf"
GUIDELINE_CHUNK_ID = "hyperkalemia::aha-2024::03"

# PRP-17: a chart document referenced by its stable OpenEMR id (no upload).
CHART_DOC_ID = "1849"


# ---------------------------------------------------------------------------
# Stub workers (no VLM / no RAG models) + stub synthesizer (no Anthropic)
# ---------------------------------------------------------------------------


def _lab_ingest_result() -> IngestResult:
    citation = SourceCitation(
        source_type="lab_pdf",
        source_id=LAB_DOC_ID,
        page_or_section="1",
        field_or_chunk_id="bbox=0.1,0.2,0.3,0.4",
        quote_or_value="6.1",
    )
    report = LabReport(
        patient_ref=SourceRef(resource_type="Patient", id="pat-1"),
        report_date=date(2026, 6, 30),
        observations=[
            LabObservation(
                test_name="Potassium",
                value="6.1",
                unit="mmol/L",
                reference_range="3.5-5.1",
                collection_date=date(2026, 6, 30),
                abnormal_flag="critical",
                citation=citation,
            )
        ],
        extraction_confidence=0.9,
        source=SourceCitation(
            source_type="lab_pdf",
            source_id=LAB_DOC_ID,
            page_or_section="1",
            field_or_chunk_id=None,
            quote_or_value="Comprehensive Metabolic Panel",
        ),
    )
    return IngestResult(
        source=SourceRef(resource_type="Document", id="1849"),
        extracted=report,
        record_refs=[],
        confidence=0.9,
    )


def _guideline_evidence() -> GuidelineEvidence:
    chunk = GuidelineChunk(
        chunk_id=GUIDELINE_CHUNK_ID,
        source_title="Management of Hyperkalemia",
        source_org="AHA",
        citation="AHA 2024, Hyperkalemia §3",
        section="Acute management",
        text="Potassium >6.0 mmol/L warrants urgent treatment and ECG.",
    )
    return GuidelineEvidence(chunk=chunk, score=8.2, retriever="hybrid")


def _chart_glucose_report() -> LabReport:
    """A bare LabReport as ``extract_chart_document`` returns it (PRP-17).

    The chart-read path yields the validated report directly (not an
    IngestResult), with every citation grounded to the stable OpenEMR doc id.
    """

    citation = SourceCitation(
        source_type="lab_pdf",
        source_id=CHART_DOC_ID,
        page_or_section="page 1",
        field_or_chunk_id="bbox=1.0,2.0,3.0,4.0",
        quote_or_value="168",
    )
    return LabReport(
        patient_ref=SourceRef(resource_type="Patient", id="a2372c03"),
        report_date=date(2026, 6, 30),
        observations=[
            LabObservation(
                test_name="Glucose",
                value="168",
                unit="mg/dL",
                reference_range="70-99",
                collection_date=date(2026, 6, 30),
                abnormal_flag="high",
                citation=citation,
            )
        ],
        extraction_confidence=0.9,
        source=SourceCitation(
            source_type="lab_pdf",
            source_id=CHART_DOC_ID,
            page_or_section="page 1",
            field_or_chunk_id=None,
            quote_or_value="Comprehensive Metabolic Panel",
        ),
    )


def _stub_intake(state):
    return [_lab_ingest_result() for _ in state["attachments"]]


def _stub_intake_docid(state):
    # Mirrors the chart-read worker path: one bare LabReport per doc-id attachment.
    return [_chart_glucose_report() for _ in state["attachments"]]


def _stub_evidence(state):
    return [_guideline_evidence()]


def _stub_runner(graph_input):
    return run_graph(
        graph_input,
        intake_extractor=_stub_intake,
        evidence_retriever=_stub_evidence,
    )


def _stub_runner_docid(graph_input):
    return run_graph(
        graph_input,
        intake_extractor=_stub_intake_docid,
        evidence_retriever=_stub_evidence,
    )


async def _stub_synthesize(question, record_facts, guideline_evidence):
    return GroundedSummary(
        headline="Potassium is critically high.",
        must_knows=[
            Claim(
                text="Potassium is 6.1 mmol/L (critical).",
                sources=[SourceRef(resource_type="lab_pdf", id=LAB_DOC_ID)],
            ),
            Claim(
                text="Guideline: K >6.0 warrants urgent treatment and ECG.",
                sources=[SourceRef(resource_type="guideline", id=GUIDELINE_CHUNK_ID)],
            ),
            # Ungrounded -> the gate must drop this before it reaches the wire.
            Claim(text="No treatment is needed.", sources=[]),
        ],
        caveats=["Confirm with a repeat draw."],
    )


async def _stub_synthesize_glucose(question, record_facts, guideline_evidence):
    return GroundedSummary(
        headline="Glucose is above range.",
        must_knows=[
            Claim(
                text="Glucose is 168 mg/dL (high).",
                sources=[SourceRef(resource_type="lab_pdf", id=CHART_DOC_ID)],
            ),
            Claim(
                text="Guideline: elevated glucose warrants review.",
                sources=[SourceRef(resource_type="guideline", id=GUIDELINE_CHUNK_ID)],
            ),
        ],
        caveats=["Confirm with a fasting draw."],
    )


def _client() -> TestClient:
    app.dependency_overrides[get_graph_runner] = lambda: _stub_runner
    app.dependency_overrides[get_answer_synthesizer] = lambda: _stub_synthesize
    return TestClient(app)


def _client_docid() -> TestClient:
    app.dependency_overrides[get_graph_runner] = lambda: _stub_runner_docid
    app.dependency_overrides[get_answer_synthesizer] = lambda: _stub_synthesize_glucose
    return TestClient(app)


def _ask() -> dict:
    body = {
        "question": "Is the potassium level dangerous?",
        "attachments": [{"file_path": "/tmp/lab.pdf", "doc_type": "lab_pdf"}],
    }
    try:
        resp = _client().post("/patients/pat-1/ask", json=body)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ask_docid() -> dict:
    # No attachment file — just the id of a document already in the chart.
    body = {
        "question": "Is this glucose result concerning per guidelines?",
        "attachments": [{"document_id": CHART_DOC_ID, "doc_type": "lab_pdf"}],
    }
    try:
        resp = _client_docid().post("/patients/a2372c03/ask", json=body)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ask_with_document_id_grounds_on_chart_doc() -> None:
    # PRP-17: /ask with only a document_id (no upload) grounds on that chart
    # document's values -> record_facts populated + cited to the doc id, AND
    # guideline evidence retrieved, kept as distinct fields.
    body = _ask_docid()

    assert body["record_facts"], "record_facts must be populated from the chart doc"
    assert body["guideline_evidence"]

    record_ids = {(c["source_type"], c["source_id"]) for c in body["record_facts"]}
    assert ("lab_pdf", CHART_DOC_ID) in record_ids  # cited to the chart doc id

    # The glucose value is grounded as a record fact cited to the doc id.
    glucose = [
        c
        for c in body["record_facts"]
        if c["source_id"] == CHART_DOC_ID
        and "Glucose" in c["quote_or_value"]
        and "168" in c["quote_or_value"]
    ]
    assert glucose, "Glucose 168 must be surfaced as a labeled record fact cited to the doc"

    texts = [c["text"] for c in body["answer_claims"]]
    assert "Glucose is 168 mg/dL (high)." in texts


def test_ask_separates_record_facts_from_guideline_evidence() -> None:
    body = _ask()

    # Distinct top-level fields, each carrying the right kind of citation.
    assert body["record_facts"]
    assert body["guideline_evidence"]
    assert all(
        c["source_type"] in {"lab_pdf", "intake_form", "fhir"} for c in body["record_facts"]
    )
    assert all(c["source_type"] == "guideline" for c in body["guideline_evidence"])

    record_ids = {(c["source_type"], c["source_id"]) for c in body["record_facts"]}
    guideline_ids = {(c["source_type"], c["source_id"]) for c in body["guideline_evidence"]}
    assert record_ids.isdisjoint(guideline_ids)
    assert ("lab_pdf", LAB_DOC_ID) in record_ids
    assert ("guideline", GUIDELINE_CHUNK_ID) in guideline_ids


def test_ask_drops_ungrounded_claim_and_grounds_the_rest() -> None:
    body = _ask()

    texts = [c["text"] for c in body["answer_claims"]]
    assert "No treatment is needed." not in texts  # ungrounded -> dropped
    assert "Potassium is 6.1 mmol/L (critical)." in texts
    assert "Guideline: K >6.0 warrants urgent treatment and ECG." in texts

    # Every surfaced claim carries a citation resolvable to the returned evidence.
    available = {
        (c["source_type"], c["source_id"])
        for c in (*body["record_facts"], *body["guideline_evidence"])
    }
    assert body["answer_claims"]
    for claim in body["answer_claims"]:
        assert claim["sources"]
        for src in claim["sources"]:
            assert (src["resource_type"], src["id"]) in available


def test_ask_returns_the_handoff_log() -> None:
    body = _ask()

    assert "handoffs" in body
    route = [h["to_node"] for h in body["handoffs"]]
    assert route == ["intake_extractor", "evidence_retriever", "done"]
    for h in body["handoffs"]:
        assert h["from_node"] == "supervisor"
        assert h["reason"]
        assert h["at"]


# ---------------------------------------------------------------------------
# Per-encounter observability metrics (FR-9): all seven signals populated
# ---------------------------------------------------------------------------


def test_build_encounter_metrics_populates_all_seven_signals() -> None:
    """The metrics assembled from a real graph run + assembly carry every
    required signal: tool sequence, step + per-worker latency, tokens/cost,
    retrieval hits, extraction confidence, and the eval outcome."""

    graph_input = GraphInput(
        patient_id="pat-1",
        question="Is the potassium level dangerous?",
        attachments=[Attachment(file_path="/tmp/lab.pdf", doc_type="lab_pdf")],
    )
    result = _stub_runner(graph_input)
    diagnostics = AnswerDiagnostics()
    answer = asyncio.run(
        build_answer(result, synthesize=_stub_synthesize, diagnostics=diagnostics)
    )
    metrics = build_encounter_metrics(
        result,
        answer,
        diagnostics,
        graph_latency_ms=1.0,
        answer_latency_ms=2.0,
        total_latency_ms=3.0,
    )

    # 1. tool sequence — the two workers, in supervisor-routed order (no 'done').
    assert metrics.tool_sequence == ["intake_extractor", "evidence_retriever"]
    assert metrics.routing_decisions[0] == "supervisor->intake_extractor"
    assert metrics.steps == 3
    # 2. latency by step + per-worker latency (both workers timed, non-negative).
    assert [s.step for s in metrics.step_latencies] == ["graph_run", "answer_build"]
    assert metrics.total_latency_ms == 3.0
    assert {w.worker for w in metrics.worker_latencies} == {
        "intake_extractor",
        "evidence_retriever",
    }
    assert all(w.latency_ms >= 0.0 and w.success for w in metrics.worker_latencies)
    # 3. token usage + cost — stub synthesizer => no live LLM => zero / None.
    assert metrics.input_tokens == 0 and metrics.output_tokens == 0
    assert metrics.cost_usd is None
    # 4. retrieval hits — the one retrieved chunk was cited by a surviving claim.
    assert metrics.retrieval_hit_rate == 1.0
    # 5. extraction confidence — mean of the extracted lab report (0.9).
    assert metrics.extraction_confidence == pytest.approx(0.9)
    # 6. answer shape — one ungrounded claim dropped by the gate, two survived.
    assert metrics.claims == 2
    assert metrics.dropped_claims == 1
    # 7. eval outcome — grounding held (claims survived) => pass.
    assert metrics.eval_outcome == "pass"


def test_ask_endpoint_emits_encounter_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving the HTTP endpoint emits exactly one per-encounter metrics event,
    tagged with the patient + the routed tool sequence."""

    captured: list[object] = []
    monkeypatch.setattr(
        "copilot.api.w2flow.record_encounter_metrics", lambda m: captured.append(m)
    )

    _ask()

    assert len(captured) == 1
    metrics = captured[0]
    assert metrics.patient_id == "pat-1"
    assert metrics.tool_sequence == ["intake_extractor", "evidence_retriever"]
    assert metrics.steps == 3
    assert metrics.claims == 2 and metrics.dropped_claims == 1
    assert metrics.eval_outcome == "pass"
    assert metrics.total_latency_ms is not None and metrics.total_latency_ms >= 0.0
