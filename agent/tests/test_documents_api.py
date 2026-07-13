"""HTTP-surface tests for the ingestion endpoint (PRP-06).

``POST /patients/{patient_id}/documents`` is driven through FastAPI's
``TestClient`` against the **real** composed tool, with the VLM stubbed
(Anthropic HTTP intercepted by ``respx``) and OpenEMR mocked (Standard REST API
intercepted by the same transport). No key and no live stack are used.

Coverage (the PRP's validation gates):

* the happy path uploads a real fixture PDF and gets back the
  :class:`IngestResult` JSON — a validated ``LabReport`` with a citation on every
  value, the stored source ref, and the derived ``record_refs``;
* an unknown ``doc_type`` is rejected with **422** before any work.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import httpx
import respx
from anthropic import AsyncAnthropic
from fastapi.testclient import TestClient

from copilot.api.documents import get_ingestor
from copilot.config import Settings
from copilot.documents.extract import (
    LabExtraction,
    LabObservationExtraction,
    VLMExtractor,
)
from copilot.documents.ingest import attach_and_extract
from copilot.documents.openemr_write import OpenEmrRestClient, OpenEmrWriter
from copilot.main import app

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
VLM_MODEL = "claude-opus-4-8"
API = "http://oemr.test/apis/default/api"
DOCS_DIR = Path(__file__).resolve().parent / "fixtures" / "documents"
LAB_PDF = DOCS_DIR / "lab_cmp_lipid.pdf"

PUUID = "pat-uuid-1"
PID = 42
DOC_ID = 1849
EID = 1850
LAB_SHA = hashlib.sha256(LAB_PDF.read_bytes()).hexdigest()

SETTINGS = Settings(openemr_base_url="http://oemr.test", anthropic_api_key="sk-ant-real")


# ---------------------------------------------------------------------------
# Composition wiring (built inside the request thread to avoid cross-loop reuse)
# ---------------------------------------------------------------------------


class _StubTokens:
    def get_access_token(self) -> str:
        return "write-token"


def _override_ingestor():
    async def _ingest(patient_id: str, file_path: str, doc_type: str):
        extractor = VLMExtractor(
            settings=SETTINGS,
            client=AsyncAnthropic(api_key="test-key", max_retries=0),
            model=VLM_MODEL,
        )
        writer = OpenEmrWriter(OpenEmrRestClient(_StubTokens(), settings=SETTINGS))
        return await attach_and_extract(
            patient_id, file_path, doc_type, extractor=extractor, writer=writer
        )

    return _ingest


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


def _mock_openemr() -> None:
    respx.get(f"{API}/patient/{PUUID}").mock(
        return_value=httpx.Response(200, json={"data": {"id": PID, "uuid": PUUID}})
    )
    respx.get(f"{API}/patient/{PID}/document").mock(
        side_effect=[
            httpx.Response(404, text=""),
            httpx.Response(
                200, json=[{"filename": f"copilot_{LAB_SHA}.pdf", "id": DOC_ID, "hash": "h"}]
            ),
        ]
    )
    respx.post(f"{API}/patient/{PID}/document").mock(
        return_value=httpx.Response(200, text="true")
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


# ---------------------------------------------------------------------------
# Happy path — 200 + validated, cited IngestResult JSON
# ---------------------------------------------------------------------------


@respx.mock
def test_ingest_endpoint_returns_cited_result() -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=_vlm_message(_lab_extraction().model_dump_json())
    )
    _mock_openemr()

    app.dependency_overrides[get_ingestor] = _override_ingestor
    try:
        with open(LAB_PDF, "rb") as fh:
            resp = TestClient(app).post(
                f"/patients/{PUUID}/documents",
                files={"file": ("lab_cmp_lipid.pdf", fh, "application/pdf")},
                data={"doc_type": "lab_pdf"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Source doc landed.
    assert body["source"] == {"resource_type": "Document", "id": str(DOC_ID), "timestamp": None}

    # Validated LabReport with a citation on every value.
    observations = body["extracted"]["observations"]
    assert len(observations) == 2
    for obs in observations:
        cit = obs["citation"]
        assert cit["source_type"] == "lab_pdf"
        assert cit["field_or_chunk_id"].startswith("bbox=")
        assert cit["quote_or_value"]

    # Derived records: ingestion encounter + a vital per value.
    refs = body["record_refs"]
    assert {"resource_type": "Encounter", "id": str(EID), "timestamp": None} in refs
    assert sum(1 for r in refs if r["resource_type"] == "Vitals") == 2

    assert body["confidence"] == 0.9


# ---------------------------------------------------------------------------
# Unknown doc_type — 422 before any work
# ---------------------------------------------------------------------------


def test_unknown_doc_type_is_422() -> None:
    resp = TestClient(app).post(
        f"/patients/{PUUID}/documents",
        files={"file": ("note.pdf", b"%PDF-1.4 bytes", "application/pdf")},
        data={"doc_type": "radiology_dicom"},
    )
    assert resp.status_code == 422
