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

from datetime import date

from fastapi.testclient import TestClient

from copilot.api.w2flow import get_answer_synthesizer, get_graph_runner
from copilot.documents.ingest import IngestResult
from copilot.documents.schemas import LabObservation, LabReport, SourceCitation
from copilot.graph.supervisor import run_graph
from copilot.main import app
from copilot.rag.chunk import GuidelineChunk
from copilot.rag.retrieve import GuidelineEvidence
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

LAB_DOC_ID = "lab_cmp_lipid.pdf"
GUIDELINE_CHUNK_ID = "hyperkalemia::aha-2024::03"


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


def _stub_intake(state):
    return [_lab_ingest_result() for _ in state["attachments"]]


def _stub_evidence(state):
    return [_guideline_evidence()]


def _stub_runner(graph_input):
    return run_graph(
        graph_input,
        intake_extractor=_stub_intake,
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


def _client() -> TestClient:
    app.dependency_overrides[get_graph_runner] = lambda: _stub_runner
    app.dependency_overrides[get_answer_synthesizer] = lambda: _stub_synthesize
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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
