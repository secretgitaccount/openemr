"""Tests for the grounded W2 answer assembly (PRP-10, FR-6).

The LLM synthesis step is **stubbed** and retrieval/extraction are supplied as
in-memory fixtures — no Anthropic key, no VLM, no network. These pin the PRP-10
contract:

* ``record_facts`` (patient documents / chart) and ``guideline_evidence`` (RAG
  corpus) are surfaced as **distinct** fields and never merged;
* an **ungrounded** claim (no citation) is dropped by the Week-1 gate — it is
  absent from the assembled answer;
* every surfaced claim carries a citation that resolves to one of the two
  evidence lists;
* a "missing data" question yields a safe, grounded partial answer — a
  hallucinated record value is dropped, not surfaced;
* the inspectable handoff log is carried through to the answer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from copilot.documents.ingest import IngestResult
from copilot.documents.schemas import (
    LabObservation,
    LabReport,
    SourceCitation,
)
from copilot.graph.answer import W2Answer, build_answer
from copilot.graph.state import GraphResult, Handoff
from copilot.rag.chunk import GuidelineChunk
from copilot.rag.retrieve import GuidelineEvidence
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

# --- source ids the fixtures + claims agree on -----------------------------

LAB_DOC_ID = "lab_cmp_lipid.pdf"
GUIDELINE_CHUNK_ID = "hyperkalemia::aha-2024::03"
GHOST_DOC_ID = "ghost_never_retrieved.pdf"


# ---------------------------------------------------------------------------
# Fixtures (in-memory; no VLM / no RAG models)
# ---------------------------------------------------------------------------


def _lab_ingest_result() -> IngestResult:
    """A PRP-06 IngestResult whose LabReport carries per-value citations."""

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
        record_refs=[SourceRef(resource_type="Encounter", id="1850")],
        confidence=0.9,
    )


def _guideline_evidence() -> GuidelineEvidence:
    """A PRP-08 GuidelineEvidence with a citable chunk."""

    chunk = GuidelineChunk(
        chunk_id=GUIDELINE_CHUNK_ID,
        source_title="Management of Hyperkalemia",
        source_org="AHA",
        citation="AHA 2024, Hyperkalemia §3",
        section="Acute management",
        text="Potassium >6.0 mmol/L warrants urgent treatment and ECG.",
    )
    return GuidelineEvidence(chunk=chunk, score=8.2, retriever="hybrid")


def _handoffs() -> list[Handoff]:
    now = datetime.now(UTC)
    return [
        Handoff(
            from_node="supervisor",
            to_node="intake_extractor",
            reason="a document is attached and not yet extracted -> intake_extractor",
            at=now,
        ),
        Handoff(
            from_node="supervisor",
            to_node="evidence_retriever",
            reason="question needs guideline support and none retrieved -> evidence_retriever",
            at=now,
        ),
        Handoff(
            from_node="supervisor",
            to_node="done",
            reason="extraction and evidence needs satisfied -> done",
            at=now,
        ),
    ]


def _graph_result(*, extracted: list, evidence: list) -> GraphResult:
    return GraphResult(
        correlation_id="corr-answer-1",
        patient_id="pat-1",
        question="Is the potassium level dangerous?",
        extracted=extracted,
        evidence=evidence,
        handoffs=_handoffs(),
        done=True,
        steps=3,
    )


# --- stub synthesizers (no Anthropic call) ---------------------------------


def _record_ref() -> SourceRef:
    return SourceRef(resource_type="lab_pdf", id=LAB_DOC_ID)


def _guideline_ref() -> SourceRef:
    return SourceRef(resource_type="guideline", id=GUIDELINE_CHUNK_ID)


async def _stub_synthesize_mixed(question, record_facts, guideline_evidence):
    """One grounded record claim, one grounded guideline claim, one ungrounded."""

    return GroundedSummary(
        headline="Potassium is critically high.",
        must_knows=[
            Claim(text="Potassium is 6.1 mmol/L (critical).", sources=[_record_ref()]),
            Claim(
                text="Guideline: K >6.0 warrants urgent treatment and ECG.",
                sources=[_guideline_ref()],
            ),
            # Ungrounded: no citation -> the gate must drop this one.
            Claim(text="The patient should be fine without treatment.", sources=[]),
        ],
        caveats=["Confirm with a repeat draw."],
    )


async def _stub_synthesize_hallucinated(question, record_facts, guideline_evidence):
    """A guideline-grounded claim + a hallucinated record value citing a ghost doc."""

    return GroundedSummary(
        headline="Answering from guideline evidence only.",
        must_knows=[
            Claim(
                text="Guideline: K >6.0 warrants urgent treatment and ECG.",
                sources=[_guideline_ref()],
            ),
            # Hallucinated: cites a record that was never retrieved -> dropped.
            Claim(
                text="Sodium is 120 mmol/L (critical).",
                sources=[SourceRef(resource_type="lab_pdf", id=GHOST_DOC_ID)],
            ),
        ],
        caveats=["No lab values are on file for this patient."],
    )


# ---------------------------------------------------------------------------
# record_facts vs guideline_evidence are distinct, never merged
# ---------------------------------------------------------------------------


async def test_record_facts_and_guideline_evidence_are_separate_fields() -> None:
    result = _graph_result(
        extracted=[_lab_ingest_result()], evidence=[_guideline_evidence()]
    )

    answer = await build_answer(result, synthesize=_stub_synthesize_mixed)

    assert isinstance(answer, W2Answer)

    # Both populated, and they are DIFFERENT fields (not one merged list).
    assert answer.record_facts
    assert answer.guideline_evidence

    # Record facts are all patient-record citations; none are guideline.
    assert all(c.source_type in {"lab_pdf", "intake_form", "fhir"} for c in answer.record_facts)
    # Guideline evidence is all guideline citations; none are record facts.
    assert all(c.source_type == "guideline" for c in answer.guideline_evidence)

    # No citation appears in both lists — the two are strictly disjoint.
    record_ids = {(c.source_type, c.source_id) for c in answer.record_facts}
    guideline_ids = {(c.source_type, c.source_id) for c in answer.guideline_evidence}
    assert record_ids.isdisjoint(guideline_ids)

    # The lab document citation is a record fact, the AHA chunk a guideline.
    assert ("lab_pdf", LAB_DOC_ID) in record_ids
    assert ("guideline", GUIDELINE_CHUNK_ID) in guideline_ids


# ---------------------------------------------------------------------------
# The gate drops an ungrounded claim
# ---------------------------------------------------------------------------


async def test_ungrounded_claim_is_dropped_by_the_gate() -> None:
    result = _graph_result(
        extracted=[_lab_ingest_result()], evidence=[_guideline_evidence()]
    )

    answer = await build_answer(result, synthesize=_stub_synthesize_mixed)

    texts = [c.text for c in answer.answer_claims]
    # The ungrounded assertion (no citation) is absent from the surfaced answer.
    assert "The patient should be fine without treatment." not in texts
    # The two grounded claims survived.
    assert "Potassium is 6.1 mmol/L (critical)." in texts
    assert "Guideline: K >6.0 warrants urgent treatment and ECG." in texts


# ---------------------------------------------------------------------------
# Every surfaced claim carries a citation resolvable to the evidence
# ---------------------------------------------------------------------------


async def test_every_surfaced_claim_has_a_source_citation() -> None:
    result = _graph_result(
        extracted=[_lab_ingest_result()], evidence=[_guideline_evidence()]
    )

    answer = await build_answer(result, synthesize=_stub_synthesize_mixed)

    available = {
        (c.source_type, c.source_id)
        for c in (*answer.record_facts, *answer.guideline_evidence)
    }
    assert answer.answer_claims  # something survived
    for claim in answer.answer_claims:
        assert claim.sources, "a surfaced claim must carry at least one citation"
        for src in claim.sources:
            assert (src.resource_type, src.id) in available


# ---------------------------------------------------------------------------
# Missing-data question -> safe grounded partial answer (no hallucinated value)
# ---------------------------------------------------------------------------


async def test_missing_data_question_yields_safe_partial_answer() -> None:
    # No documents extracted; only guideline evidence retrieved.
    result = _graph_result(extracted=[], evidence=[_guideline_evidence()])

    answer = await build_answer(result, synthesize=_stub_synthesize_hallucinated)

    # No patient-record facts were available, so none are surfaced (not invented).
    assert answer.record_facts == []

    texts = [c.text for c in answer.answer_claims]
    # The hallucinated record value (citing a never-retrieved doc) is dropped.
    assert "Sodium is 120 mmol/L (critical)." not in texts
    # The guideline-grounded claim stands, and the caveat is surfaced.
    assert "Guideline: K >6.0 warrants urgent treatment and ECG." in texts
    assert "No lab values are on file for this patient." in answer.caveats

    # Whatever remains is still fully grounded.
    guideline_ids = {(c.source_type, c.source_id) for c in answer.guideline_evidence}
    for claim in answer.answer_claims:
        for src in claim.sources:
            assert (src.resource_type, src.id) in guideline_ids


# ---------------------------------------------------------------------------
# The handoff log is carried through to the answer
# ---------------------------------------------------------------------------


async def test_handoff_log_is_returned_in_the_answer() -> None:
    result = _graph_result(
        extracted=[_lab_ingest_result()], evidence=[_guideline_evidence()]
    )

    answer = await build_answer(result, synthesize=_stub_synthesize_mixed)

    assert answer.handoffs == result.handoffs
    route = [h.to_node for h in answer.handoffs]
    assert route == ["intake_extractor", "evidence_retriever", "done"]
