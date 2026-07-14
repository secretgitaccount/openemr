"""HTTP-surface tests for the ingest-from-chart endpoints (PRP-15).

Driven through FastAPI's ``TestClient`` with the chart reader / ingestor
dependencies overridden by versions wired to a stub token + ``respx``-mocked
OpenEMR FHIR + a stubbed VLM. No API key and no live stack are used.

Coverage (the PRP's validation gates):

* ``GET  /patients/{id}/chart-documents`` returns the mapped ``ChartDocument``\\ s;
* ``POST /patients/{id}/chart-documents/{doc_id}/ingest`` returns the cited
  :class:`IngestResult` whose citations carry ``source_id == doc_id``;
* an unknown ``doc_type`` is rejected with **422** before any work;
* ``GET  /patients/{id}/chart-documents/{doc_id}/page/{n}`` returns a PNG
  data-URI + pixel/point dims.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx
from anthropic import AsyncAnthropic
from fastapi.testclient import TestClient

from copilot.api.chart_documents import get_chart_ingestor, get_chart_reader
from copilot.config import Settings
from copilot.documents.chart_read import ChartReader, ingest_chart_document
from copilot.documents.extract import (
    LabExtraction,
    LabObservationExtraction,
    VLMExtractor,
)
from copilot.documents.openemr_write import OpenEmrRestClient, OpenEmrWriter
from copilot.main import app

FHIR = "http://oemr.test/apis/default/fhir"
API = "http://oemr.test/apis/default/api"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
VLM_MODEL = "claude-opus-4-8"

DOCS_DIR = Path(__file__).resolve().parent / "fixtures" / "documents"
LAB_PDF = DOCS_DIR / "lab_cmp_lipid.pdf"
LAB_BYTES = LAB_PDF.read_bytes()

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


async def _override_reader() -> ChartReader:
    return ChartReader(_StubTokens(), settings=SETTINGS)


def _bundle() -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {
                "resource": {
                    "resourceType": "DocumentReference",
                    "id": "dr-1",
                    "description": "Front-desk Lab Upload",
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
            }
        ],
    }


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


def _mock_binary_and_writes() -> None:
    respx.get(f"{FHIR}/Binary/{DOC_ID}").mock(
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


def _override_ingestor():
    async def _ingest(patient_id: str, doc_id: str, doc_type: str):
        reader = ChartReader(_StubTokens(), settings=SETTINGS)
        extractor = VLMExtractor(
            settings=SETTINGS,
            client=AsyncAnthropic(api_key="test-key", max_retries=0),
            model=VLM_MODEL,
        )
        writer = OpenEmrWriter(OpenEmrRestClient(_StubTokens(), settings=SETTINGS))
        return await ingest_chart_document(
            patient_id, doc_id, doc_type, reader=reader, extractor=extractor, writer=writer
        )

    return _ingest


# ---------------------------------------------------------------------------
# GET /chart-documents
# ---------------------------------------------------------------------------


@respx.mock
def test_list_endpoint_returns_chart_documents() -> None:
    respx.get(f"{FHIR}/DocumentReference").mock(
        return_value=httpx.Response(200, json=_bundle())
    )
    app.dependency_overrides[get_chart_reader] = _override_reader
    try:
        resp = TestClient(app).get(f"/patients/{PUUID}/chart-documents")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["doc_id"] == DOC_ID
    assert body[0]["title"] == "Front-desk Lab Upload"
    assert body[0]["mimetype"] == "application/pdf"


# ---------------------------------------------------------------------------
# POST /chart-documents/{doc_id}/ingest
# ---------------------------------------------------------------------------


@respx.mock
def test_ingest_endpoint_returns_cited_result() -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=_vlm_message(_lab_extraction().model_dump_json())
    )
    _mock_binary_and_writes()

    app.dependency_overrides[get_chart_ingestor] = _override_ingestor
    try:
        resp = TestClient(app).post(
            f"/patients/{PUUID}/chart-documents/{DOC_ID}/ingest",
            params={"doc_type": "lab_pdf"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Source is the existing chart document.
    assert body["source"] == {"resource_type": "Document", "id": DOC_ID, "timestamp": None}

    # Every citation grounded to the OpenEMR doc id.
    observations = body["extracted"]["observations"]
    assert len(observations) == 2
    for obs in observations:
        assert obs["citation"]["source_id"] == DOC_ID
    assert body["extracted"]["source"]["source_id"] == DOC_ID

    # Derived records persisted.
    refs = body["record_refs"]
    assert {"resource_type": "Encounter", "id": str(EID), "timestamp": None} in refs
    assert sum(1 for r in refs if r["resource_type"] == "Vitals") == 2
    assert body["confidence"] == 0.9


def test_ingest_endpoint_unknown_doc_type_is_422() -> None:
    resp = TestClient(app).post(
        f"/patients/{PUUID}/chart-documents/{DOC_ID}/ingest",
        params={"doc_type": "radiology_dicom"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /chart-documents/{doc_id}/page/{n}
# ---------------------------------------------------------------------------


@respx.mock
def test_page_endpoint_returns_png_and_dims() -> None:
    respx.get(f"{FHIR}/Binary/{DOC_ID}").mock(
        return_value=httpx.Response(
            200, content=LAB_BYTES, headers={"content-type": "application/pdf"}
        )
    )
    app.dependency_overrides[get_chart_reader] = _override_reader
    try:
        resp = TestClient(app).get(f"/patients/{PUUID}/chart-documents/{DOC_ID}/page/1")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page"] == 1
    assert body["is_image"] is False
    assert body["width_px"] > 0 and body["height_px"] > 0
    assert body["width_pt"] and body["height_pt"]
    assert body["image_data_uri"].startswith("data:image/png;base64,")
