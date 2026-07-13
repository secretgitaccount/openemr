"""Unit tests for the VLM document extractor (PRP-04, FR-6).

The Anthropic HTTP is mocked with ``respx`` (the async httpx transport is
intercepted globally) so the **real** ``messages.parse`` runs — it binds the
call to the extraction schema and validates the (stubbed) response, exactly as
it would against a live VLM. No ``ANTHROPIC_API_KEY`` is used anywhere here; the
live smoke lives in PRP-06.

Coverage (the PRP's validation gates):

* a valid stubbed payload → a validated :class:`LabReport` with a
  :class:`SourceCitation` (page + word box + quote) on **every** value;
* a malformed stubbed payload → schema rejection surfaced as a typed
  :class:`ExtractionError` (never a silent pass, and not retried);
* the image-only fixture (``lab_scanned.png``, no text layer) → extraction still
  works, degrading to **page-level** citations (graceful degradation);
* the outbound call attaches the JSON schema + vision image and targets the
  configured Opus-4.8 vision model;
* a refusal and a missing key both surface typed errors rather than fabricating.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic

from copilot.config import Settings
from copilot.documents.extract import (
    CitedListExtraction,
    DemographicsExtraction,
    ExtractionError,
    IntakeExtraction,
    LabExtraction,
    LabObservationExtraction,
    VLMExtractor,
)
from copilot.documents.schemas import IntakeFacts, LabReport, SourceCitation

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
VLM_MODEL = "claude-opus-4-8"
DOCS_DIR = Path(__file__).resolve().parent / "fixtures" / "documents"
LAB_PDF = DOCS_DIR / "lab_cmp_lipid.pdf"
INTAKE_PDF = DOCS_DIR / "intake_form.pdf"
LAB_PNG = DOCS_DIR / "lab_scanned.png"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    # A real-looking key so the injected-client path is exercised without one.
    return Settings(anthropic_api_key="sk-ant-real")


def _anthropic() -> AsyncAnthropic:
    # max_retries=0 so the SDK's own retry doesn't mask the extractor's tenacity.
    return AsyncAnthropic(api_key="test-key", max_retries=0)


def _extractor(settings: Settings) -> VLMExtractor:
    return VLMExtractor(settings=settings, client=_anthropic(), model=VLM_MODEL)


def _lab_extraction() -> LabExtraction:
    # `quote` values are verbatim single words present in lab_cmp_lipid.pdf, so
    # they resolve to word boxes; `source_quote` is a header word likewise.
    return LabExtraction(
        patient_mrn="FAKE-000123",
        report_date=date(2026, 6, 30),
        observations=[
            LabObservationExtraction(
                test_name="Glucose",
                value="168",
                unit="mg/dL",
                reference_range="70-99",
                collection_date=date(2026, 6, 30),
                abnormal_flag="high",
                quote="168",
            ),
            LabObservationExtraction(
                test_name="Total Cholesterol",
                value="232",
                unit="mg/dL",
                reference_range="<200",
                collection_date=None,
                abnormal_flag="high",
                quote="232",
            ),
        ],
        extraction_confidence=0.9,
        source_quote="Comprehensive",
    )


def _intake_extraction() -> IntakeExtraction:
    return IntakeExtraction(
        demographics=DemographicsExtraction(
            name="Jordan Q. Testpatient", dob=date(1968, 3, 14), sex="Male", quote="Demographics"
        ),
        chief_concern="Follow-up for type 2 diabetes",
        chief_concern_quote="Concern",
        current_medications=CitedListExtraction(
            items=["Metformin 1000 mg PO BID"], quote="Medications"
        ),
        allergies=CitedListExtraction(items=["Penicillin (hives)"], quote="Allergies"),
        family_history=CitedListExtraction(items=["Mother: hypertension"], quote="Family"),
        extraction_confidence=0.85,
        source_quote="Demographics",
    )


def _message_body(text: str | None, *, stop_reason: str = "end_turn") -> dict:
    content = [] if text is None else [{"type": "text", "text": text}]
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": VLM_MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 500, "output_tokens": 120},
    }


# ---------------------------------------------------------------------------
# Valid payload -> grounded LabReport (schema is the source of truth)
# ---------------------------------------------------------------------------


@respx.mock
async def test_valid_payload_yields_labreport_cited_on_every_value(settings: Settings) -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_message_body(_lab_extraction().model_dump_json()))
    )

    report = await _extractor(settings).extract_lab(LAB_PDF)

    assert isinstance(report, LabReport)
    assert len(report.observations) == 2
    # Every extracted value carries a SourceCitation with page + box + quote.
    for obs in report.observations:
        assert isinstance(obs.citation, SourceCitation)
        assert obs.citation.source_type == "lab_pdf"
        assert obs.citation.source_id == "lab_cmp_lipid.pdf"
        assert obs.citation.page_or_section == "page 1"
        assert obs.citation.field_or_chunk_id is not None  # a real bounding box
        assert obs.citation.field_or_chunk_id.startswith("bbox=")
        assert obs.citation.quote_or_value  # the verbatim value read
    # Report-level grounding + downstream-resolved patient pointer.
    assert report.source.field_or_chunk_id is not None
    assert report.patient_ref.id == "FAKE-000123"
    # All values grounded -> confidence is the VLM self-report unclamped.
    assert report.extraction_confidence == pytest.approx(0.9)


@respx.mock
async def test_request_attaches_schema_and_vision_and_targets_opus(settings: Settings) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_message_body(_lab_extraction().model_dump_json()))
    )

    await _extractor(settings).extract_lab(LAB_PDF)

    body = json.loads(route.calls.last.request.content)
    assert body["model"] == VLM_MODEL
    # Structured output bound to the extraction schema.
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "schema" in body["output_config"]["format"]
    # Vision: at least one rendered page image is attached.
    blocks = body["messages"][0]["content"]
    assert any(b.get("type") == "image" for b in blocks)


# ---------------------------------------------------------------------------
# Malformed payload -> schema rejection (never a silent pass, not retried)
# ---------------------------------------------------------------------------


@respx.mock
async def test_malformed_payload_is_rejected_by_schema(settings: Settings) -> None:
    # Valid JSON, but `observations` is not a list -> ValidationError in parse.
    bad = json.dumps(
        {
            "patient_mrn": None,
            "report_date": None,
            "observations": "not-a-list",
            "extraction_confidence": 0.5,
            "source_quote": "x",
        }
    )
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=_message_body(bad)))

    with pytest.raises(ExtractionError) as excinfo:
        await _extractor(settings).extract_lab(LAB_PDF)

    assert excinfo.value.retriable is False
    # A schema violation is permanent — it is surfaced, not retried away.
    assert route.call_count == 1


@respx.mock
async def test_refusal_surfaces_typed_error(settings: Settings) -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_message_body(None, stop_reason="refusal"))
    )

    with pytest.raises(ExtractionError) as excinfo:
        await _extractor(settings).extract_lab(LAB_PDF)
    assert excinfo.value.retriable is False


# ---------------------------------------------------------------------------
# Image-only fixture -> still works, degrading to page-level citations
# ---------------------------------------------------------------------------


@respx.mock
async def test_image_only_doc_degrades_to_page_level_citation(settings: Settings) -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_message_body(_lab_extraction().model_dump_json()))
    )

    report = await _extractor(settings).extract_lab(LAB_PNG)

    assert isinstance(report, LabReport)
    assert report.observations, "extraction should still produce observations"
    # No text layer -> no word boxes -> page-level (never box-level, never dropped).
    for obs in report.observations:
        assert obs.citation.page_or_section == "page 1"
        assert obs.citation.field_or_chunk_id is None
        assert obs.citation.quote_or_value  # still grounded to the value it read
    # Nothing grounded to a box -> confidence clamped to 0.0 (honest degradation).
    assert report.extraction_confidence == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Retry (transient failure) + intake path + missing-key surfacing
# ---------------------------------------------------------------------------


@respx.mock
async def test_transient_5xx_is_retried_then_succeeds(settings: Settings) -> None:
    route = respx.post(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(
                503,
                json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            ),
            httpx.Response(200, json=_message_body(_lab_extraction().model_dump_json())),
        ]
    )

    report = await _extractor(settings).extract_lab(LAB_PDF)

    assert isinstance(report, LabReport)
    assert route.call_count == 2


@respx.mock
async def test_intake_extraction_is_grounded(settings: Settings) -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_message_body(_intake_extraction().model_dump_json()))
    )

    facts = await _extractor(settings).extract_intake(INTAKE_PDF)

    assert isinstance(facts, IntakeFacts)
    assert facts.demographics.citation.source_type == "intake_form"
    assert facts.current_medications.items == ["Metformin 1000 mg PO BID"]
    assert facts.chief_concern is not None
    # Each grounded fact carries a citation with a verbatim quote.
    assert facts.allergies.citation.quote_or_value
    assert 0.0 <= facts.extraction_confidence <= 1.0


async def test_missing_key_surfaced_only_on_live_call() -> None:
    # Placeholder key + no injected client -> a clear error when a call is tried
    # (rendering the document first succeeds; the key check fires at call time).
    extractor = VLMExtractor(settings=Settings(anthropic_api_key="sk-ant-xxxxxxxx"))
    with pytest.raises(ExtractionError) as excinfo:
        await extractor.extract_lab(LAB_PDF)
    assert excinfo.value.retriable is False
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
