"""Unit tests for the ingest-from-chart read path (PRP-15).

OpenEMR FHIR is mocked with ``respx`` (the async httpx transport is intercepted
globally) and the VLM is stubbed (Anthropic HTTP intercepted by the same
transport). No API key and no live stack are used.

Coverage (the PRP's validation gates):

* :func:`ChartReader.list_chart_documents` maps a FHIR ``DocumentReference``
  Bundle into :class:`ChartDocument`\\ s, keyed on the ``Binary/<id>`` id, and
  skips a resource whose bytes can't be located;
* :func:`ChartReader.fetch_document_bytes` returns the decrypted raw bytes (and
  decodes a FHIR ``Binary`` JSON envelope);
* :func:`ingest_chart_document` fetches Binary bytes → a validated
  :class:`LabReport` whose **every** citation carries ``source_id == doc_id``,
  whose ``source`` points at the existing document (no re-upload), and with the
  derived vitals persisted;
* an unknown ``doc_type`` raises :class:`IngestError` before any work.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic

from copilot.config import Settings
from copilot.documents.chart_read import (
    ChartReader,
    fetch_document_bytes,
    ingest_chart_document,
    list_chart_documents,
)
from copilot.documents.extract import (
    LabExtraction,
    LabObservationExtraction,
    VLMExtractor,
)
from copilot.documents.ingest import IngestError
from copilot.documents.openemr_write import OpenEmrRestClient, OpenEmrWriter
from copilot.documents.schemas import LabReport

FHIR = "http://oemr.test/apis/default/fhir"
API = "http://oemr.test/apis/default/api"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
VLM_MODEL = "claude-opus-4-8"

DOCS_DIR = Path(__file__).resolve().parent / "fixtures" / "documents"
LAB_PDF = DOCS_DIR / "lab_cmp_lipid.pdf"
LAB_BYTES = LAB_PDF.read_bytes()
LAB_SHA = hashlib.sha256(LAB_BYTES).hexdigest()

PUUID = "pat-uuid-1"
PID = 42
DOC_ID = "1849"
EID = 1850

SETTINGS = Settings(
    openemr_base_url="http://oemr.test",
    openemr_fhir_base=FHIR,
    anthropic_api_key="sk-ant-real",
)


class _StubTokens:
    def get_access_token(self) -> str:
        return "read-token"


def _reader() -> ChartReader:
    return ChartReader(_StubTokens(), settings=SETTINGS)


# ---------------------------------------------------------------------------
# list_chart_documents
# ---------------------------------------------------------------------------


def _bundle() -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {
                "resource": {
                    "resourceType": "DocumentReference",
                    "id": "dr-1",
                    "description": "Scanned Lab Report",
                    "type": {"text": "Laboratory report"},
                    "category": [{"coding": [{"display": "Lab Report"}]}],
                    "date": "2026-06-30T10:00:00Z",
                    "content": [
                        {
                            "attachment": {
                                "contentType": "application/pdf",
                                "url": f"{FHIR}/Binary/{DOC_ID}",
                            }
                        }
                    ],
                }
            },
            {
                # No Binary URL -> cannot fetch bytes -> must be skipped.
                "resource": {
                    "resourceType": "DocumentReference",
                    "id": "dr-2",
                    "description": "Orphan",
                    "content": [{"attachment": {"contentType": "application/pdf"}}],
                }
            },
        ],
    }


@respx.mock
async def test_list_chart_documents_maps_and_skips() -> None:
    route = respx.get(f"{FHIR}/DocumentReference").mock(
        return_value=httpx.Response(200, json=_bundle())
    )

    async with _reader() as reader:
        docs = await reader.list_chart_documents(PUUID)

    assert route.called
    # The patient filter was sent as a query param.
    assert route.calls.last.request.url.params["patient"] == PUUID
    assert len(docs) == 1
    doc = docs[0]
    assert doc.doc_id == DOC_ID
    assert doc.title == "Scanned Lab Report"
    assert doc.category == "Lab Report"
    assert doc.mimetype == "application/pdf"
    assert doc.date is not None and doc.date.startswith("2026-06-30")


@respx.mock
async def test_list_chart_documents_module_level_with_injected_reader() -> None:
    respx.get(f"{FHIR}/DocumentReference").mock(
        return_value=httpx.Response(200, json=_bundle())
    )
    async with _reader() as reader:
        docs = await list_chart_documents(PUUID, reader=reader)
    assert [d.doc_id for d in docs] == [DOC_ID]


# ---------------------------------------------------------------------------
# fetch_document_bytes
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_document_bytes_returns_raw_stream() -> None:
    route = respx.get(f"{FHIR}/Binary/{DOC_ID}").mock(
        return_value=httpx.Response(
            200, content=LAB_BYTES, headers={"content-type": "application/pdf"}
        )
    )
    data = await fetch_document_bytes(PUUID, DOC_ID, reader=_reader())
    assert route.called
    assert data == LAB_BYTES


@respx.mock
async def test_fetch_document_bytes_decodes_fhir_binary_json() -> None:
    respx.get(f"{FHIR}/Binary/{DOC_ID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "resourceType": "Binary",
                "contentType": "application/pdf",
                "data": base64.b64encode(LAB_BYTES).decode("ascii"),
            },
        )
    )
    data = await fetch_document_bytes(PUUID, DOC_ID, reader=_reader())
    assert data == LAB_BYTES


@respx.mock
async def test_fetch_document_bytes_http_error_raises() -> None:
    from copilot.documents.chart_read import ChartReadError

    respx.get(f"{FHIR}/Binary/{DOC_ID}").mock(return_value=httpx.Response(404))
    with pytest.raises(ChartReadError) as exc:
        await fetch_document_bytes(PUUID, DOC_ID, reader=_reader())
    assert exc.value.status_code == 404
    assert exc.value.retriable is False


# ---------------------------------------------------------------------------
# ingest_chart_document
# ---------------------------------------------------------------------------


def _lab_extraction() -> LabExtraction:
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


def _vlm_message(payload: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": VLM_MODEL,
            "content": [{"type": "text", "text": payload}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 500, "output_tokens": 120},
        },
    )


def _mock_binary_and_writes() -> httpx.Route:
    binary = respx.get(f"{FHIR}/Binary/{DOC_ID}").mock(
        return_value=httpx.Response(
            200, content=LAB_BYTES, headers={"content-type": "application/pdf"}
        )
    )
    respx.get(f"{API}/patient/{PUUID}").mock(
        return_value=httpx.Response(200, json={"data": {"id": PID, "uuid": PUUID}})
    )
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
    return binary


def _extractor() -> VLMExtractor:
    return VLMExtractor(
        settings=SETTINGS,
        client=AsyncAnthropic(api_key="test-key", max_retries=0),
        model=VLM_MODEL,
    )


def _writer() -> OpenEmrWriter:
    return OpenEmrWriter(OpenEmrRestClient(_StubTokens(), settings=SETTINGS))


@respx.mock
async def test_ingest_chart_document_grounds_citations_to_doc_id() -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=_vlm_message(_lab_extraction().model_dump_json())
    )
    doc_post = respx.post(f"{API}/patient/{PID}/document").mock(
        return_value=httpx.Response(200, text="true")
    )
    _mock_binary_and_writes()

    result = await ingest_chart_document(
        PUUID,
        DOC_ID,
        "lab_pdf",
        reader=_reader(),
        extractor=_extractor(),
        writer=_writer(),
    )

    # Source is the EXISTING chart document — nothing re-uploaded.
    assert result.source.resource_type == "Document"
    assert result.source.id == DOC_ID
    assert not doc_post.called

    # A validated LabReport with every citation grounded to the OpenEMR doc id.
    assert isinstance(result.extracted, LabReport)
    report = result.extracted
    assert len(report.observations) == 2
    assert report.source.source_id == DOC_ID
    for obs in report.observations:
        assert obs.citation.source_id == DOC_ID
        assert obs.citation.source_type == "lab_pdf"
        assert (obs.citation.field_or_chunk_id or "").startswith("bbox=")

    # Derived records: the ingestion encounter + a vital per value.
    kinds = [(r.resource_type, r.id) for r in result.record_refs]
    assert ("Encounter", str(EID)) in kinds
    assert sum(1 for r in result.record_refs if r.resource_type == "Vitals") == 2

    assert result.confidence == 0.9


async def test_ingest_chart_document_unknown_doc_type_raises() -> None:
    with pytest.raises(IngestError):
        await ingest_chart_document(PUUID, DOC_ID, "radiology_dicom", reader=_reader())
