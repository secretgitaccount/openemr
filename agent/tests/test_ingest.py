"""Integration tests for ``attach_and_extract`` (PRP-06), fully offline.

The composed tool (store source → extract → persist) is exercised end-to-end
with the **VLM stubbed** (Anthropic HTTP intercepted by ``respx`` so the real
``messages.parse`` runs against a canned payload) and **OpenEMR mocked** (the
Standard REST API intercepted by the same ``respx`` transport). No
``ANTHROPIC_API_KEY`` and no live stack are touched — that is the live smoke gate
documented in the PRP.

Coverage (the PRP's validation gates):

* the happy path returns a validated :class:`LabReport` with a
  :class:`SourceCitation` on **every** value, the stored source ref, and the
  derived ``record_refs`` (ingestion encounter + a vital per value);
* re-ingesting the same file does **not** duplicate — no upload, no encounter,
  no vital is re-created (FR-10 idempotency, threaded through the composition);
* an unknown ``doc_type`` is rejected with a typed :class:`IngestError` before
  any network or VLM work;
* the intake path stores its source and returns validated
  :class:`IntakeFacts` with no derived record refs (PRP-05 has no intake write).
"""

from __future__ import annotations

import hashlib
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
    IntakeExtraction,
    LabExtraction,
    LabObservationExtraction,
    VLMExtractor,
)
from copilot.documents.ingest import (
    IngestError,
    IngestResult,
    attach_and_extract,
)
from copilot.documents.openemr_write import OpenEmrRestClient, OpenEmrWriter
from copilot.documents.schemas import IntakeFacts, LabReport, SourceCitation
from copilot.schemas.core import SourceRef

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
VLM_MODEL = "claude-opus-4-8"
API = "http://oemr.test/apis/default/api"
DOCS_DIR = Path(__file__).resolve().parent / "fixtures" / "documents"
LAB_PDF = DOCS_DIR / "lab_cmp_lipid.pdf"
INTAKE_PDF = DOCS_DIR / "intake_form.pdf"

PUUID = "pat-uuid-1"
PID = 42
DOC_ID = 1849
EID = 1850

LAB_SHA = hashlib.sha256(LAB_PDF.read_bytes()).hexdigest()
INTAKE_SHA = hashlib.sha256(INTAKE_PDF.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(openemr_base_url="http://oemr.test", anthropic_api_key="sk-ant-real")


class _StubTokens:
    def get_access_token(self) -> str:
        return "write-token"


def _extractor(settings: Settings) -> VLMExtractor:
    # max_retries=0 so the SDK's own retry doesn't mask the extractor's tenacity.
    client = AsyncAnthropic(api_key="test-key", max_retries=0)
    return VLMExtractor(settings=settings, client=client, model=VLM_MODEL)


def _writer(settings: Settings) -> OpenEmrWriter:
    return OpenEmrWriter(OpenEmrRestClient(_StubTokens(), settings=settings))


def _patient_ok() -> httpx.Response:
    return httpx.Response(200, json={"data": {"id": PID, "uuid": PUUID}})


def _lab_extraction() -> LabExtraction:
    # Verbatim quotes present in lab_cmp_lipid.pdf so they resolve to word boxes.
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


def _vlm_message(payload: str) -> httpx.Response:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": VLM_MODEL,
        "content": [{"type": "text", "text": payload}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 500, "output_tokens": 120},
    }
    return httpx.Response(200, json=body)


def _mock_vlm_lab() -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=_vlm_message(_lab_extraction().model_dump_json())
    )


def _mock_store_fresh(sha: str) -> None:
    """A source not yet stored: dedup 404, upload, then recover the id."""

    respx.get(f"{API}/patient/{PUUID}").mock(return_value=_patient_ok())
    respx.get(f"{API}/patient/{PID}/document").mock(
        side_effect=[
            httpx.Response(404, text=""),  # dedup: empty folder
            httpx.Response(  # recovery after upload
                200,
                json=[{"filename": f"copilot_{sha}.pdf", "id": DOC_ID, "hash": "h"}],
            ),
        ]
    )
    respx.post(f"{API}/patient/{PID}/document").mock(
        return_value=httpx.Response(200, text="true")
    )


def _mock_persist_fresh() -> None:
    """A source with no prior ingestion encounter: create it + a vital per value."""

    respx.get(f"{API}/patient/{PUUID}/encounter").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.post(f"{API}/patient/{PUUID}/encounter").mock(
        return_value=httpx.Response(201, json={"data": {"eid": EID, "encounter": EID}})
    )
    respx.get(f"{API}/patient/{PID}/encounter/{EID}/vital").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{API}/patient/{PID}/encounter/{EID}/vital").mock(
        side_effect=[
            httpx.Response(201, json={"vid": 26, "fid": 26}),
            httpx.Response(201, json={"vid": 27, "fid": 27}),
        ]
    )


# ---------------------------------------------------------------------------
# Happy path — validated LabReport, per-value citations, record refs
# ---------------------------------------------------------------------------


@respx.mock
async def test_attach_and_extract_lab_end_to_end(settings: Settings) -> None:
    _mock_vlm_lab()
    _mock_store_fresh(LAB_SHA)
    _mock_persist_fresh()

    result = await attach_and_extract(
        PUUID,
        LAB_PDF,
        "lab_pdf",
        extractor=_extractor(settings),
        writer=_writer(settings),
    )

    assert isinstance(result, IngestResult)
    # Source doc landed.
    assert result.source == SourceRef(resource_type="Document", id=str(DOC_ID))

    # Validated LabReport with a citation on every value.
    assert isinstance(result.extracted, LabReport)
    assert len(result.extracted.observations) == 2
    for obs in result.extracted.observations:
        assert isinstance(obs.citation, SourceCitation)
        assert obs.citation.source_type == "lab_pdf"
        assert obs.citation.field_or_chunk_id.startswith("bbox=")  # box-grounded
        assert obs.citation.quote_or_value

    # Derived records: one ingestion encounter + a vital per value.
    assert SourceRef(resource_type="Encounter", id=str(EID)) in result.record_refs
    vitals = [r for r in result.record_refs if r.resource_type == "Vitals"]
    assert len(vitals) == 2

    # Confidence mirrors the (fully grounded) extraction self-report.
    assert result.confidence == pytest.approx(0.9)


@respx.mock
async def test_second_ingest_does_not_duplicate(settings: Settings) -> None:
    _mock_vlm_lab()

    # Source already stored (SHA-256 present in the listing) → no upload.
    respx.get(f"{API}/patient/{PUUID}").mock(return_value=_patient_ok())
    respx.get(f"{API}/patient/{PID}/document").mock(
        return_value=httpx.Response(
            200, json=[{"filename": f"copilot_{LAB_SHA}.pdf", "id": DOC_ID, "hash": "h"}]
        )
    )
    upload = respx.post(f"{API}/patient/{PID}/document")

    # Ingestion encounter already exists (matched by its deterministic reason),
    # and both vital notes are already present → nothing re-created.
    from copilot.documents.openemr_write import _ingest_token, _vital_note
    from copilot.documents.schemas import LabObservation

    reason = f"Clinical Co-Pilot ingest:{_ingest_token('lab_cmp_lipid.pdf')}"

    def _note(name: str, value: str, unit: str) -> str:
        return _vital_note(
            LabObservation(
                test_name=name,
                value=value,
                unit=unit,
                reference_range=None,
                collection_date=None,
                abnormal_flag="high",
                citation=SourceCitation(
                    source_type="lab_pdf", source_id="x", quote_or_value="x"
                ),
            )
        )

    respx.get(f"{API}/patient/{PUUID}/encounter").mock(
        return_value=httpx.Response(200, json={"data": [{"eid": EID, "reason": reason}]})
    )
    create_enc = respx.post(f"{API}/patient/{PUUID}/encounter")
    respx.get(f"{API}/patient/{PID}/encounter/{EID}/vital").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 26, "note": _note("Glucose", "168", "mg/dL")},
                {"id": 27, "note": _note("Total Cholesterol", "232", "mg/dL")},
            ],
        )
    )
    post_vital = respx.post(f"{API}/patient/{PID}/encounter/{EID}/vital")

    result = await attach_and_extract(
        PUUID,
        LAB_PDF,
        "lab_pdf",
        extractor=_extractor(settings),
        writer=_writer(settings),
    )

    # Same logical records returned — nothing duplicated.
    assert result.source == SourceRef(resource_type="Document", id=str(DOC_ID))
    assert not upload.called
    assert not create_enc.called
    assert not post_vital.called
    vitals = [r for r in result.record_refs if r.resource_type == "Vitals"]
    assert len(vitals) == 2


# ---------------------------------------------------------------------------
# Bad doc_type — rejected before any work
# ---------------------------------------------------------------------------


async def test_unknown_doc_type_raises_before_any_call(settings: Settings) -> None:
    with pytest.raises(IngestError):
        await attach_and_extract(PUUID, LAB_PDF, "radiology_dicom")


# ---------------------------------------------------------------------------
# Intake path — source stored, no derived record refs
# ---------------------------------------------------------------------------


@respx.mock
async def test_intake_ingest_stores_source_no_derived_refs(settings: Settings) -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=_vlm_message(_intake_extraction().model_dump_json())
    )
    respx.get(f"{API}/patient/{PUUID}").mock(return_value=_patient_ok())
    respx.get(f"{API}/patient/{PID}/document").mock(
        side_effect=[
            httpx.Response(404, text=""),
            httpx.Response(
                200, json=[{"filename": f"copilot_{INTAKE_SHA}.pdf", "id": DOC_ID, "hash": "h"}]
            ),
        ]
    )
    respx.post(f"{API}/patient/{PID}/document").mock(
        return_value=httpx.Response(200, text="true")
    )

    result = await attach_and_extract(
        PUUID,
        INTAKE_PDF,
        "intake_form",
        extractor=_extractor(settings),
        writer=_writer(settings),
    )

    assert isinstance(result.extracted, IntakeFacts)
    assert result.source == SourceRef(resource_type="Document", id=str(DOC_ID))
    assert result.record_refs == []  # no intake persistence path in PRP-05
    assert 0.0 <= result.confidence <= 1.0
