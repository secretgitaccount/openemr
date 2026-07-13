"""Unit tests for the OpenEMR write path (PRP-05), with OpenEMR mocked.

OpenEMR's Standard REST API is intercepted with ``respx`` (no network, no key).
The live-local proof (real upload + no-duplicate re-upload + a derived vital) is
the smoke gate documented in the PRP, run separately against localhost:8300.

Coverage:

* ``store_source`` happy path — returns the real-shaped document id recovered
  from the listing; the upload uses multipart field **``document``** and a
  ``path`` **query** param (both asserted), and the filename embeds the SHA-256.
* idempotent re-write — when the SHA-256 is already present in the listing, no
  POST is issued and the same logical id is returned (no duplicate).
* ``persist_observations`` happy path — creates one ingestion encounter and a
  vital per derived value, returning grounded ``SourceRef``s.
* idempotent re-persist — an existing ingestion encounter + existing vital notes
  are reused; no encounter/vital is re-created.
* error path — a rejected vital yields a typed ``OpenEmrWriteError`` with
  ``partial=True``, the failed key in ``missing``, and the encounter still in
  ``partial_results`` (never a silent drop).
* a missing patient surfaces a typed, non-retriable error.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from copilot.config import Settings
from copilot.documents.openemr_write import (
    OpenEmrRestClient,
    OpenEmrWriteError,
    OpenEmrWriter,
)
from copilot.documents.schemas import LabObservation, LabReport, SourceCitation
from copilot.schemas.core import SourceRef

API = "http://oemr.test/apis/default/api"
PUUID = "pat-uuid-1"
PID = 42


@pytest.fixture
def settings() -> Settings:
    return Settings(openemr_base_url="http://oemr.test")


class _StubTokens:
    def get_access_token(self) -> str:
        return "write-token"


def _writer(settings: Settings) -> OpenEmrWriter:
    return OpenEmrWriter(OpenEmrRestClient(_StubTokens(), settings=settings))


def _patient_ok() -> httpx.Response:
    return httpx.Response(200, json={"data": {"id": PID, "uuid": PUUID}})


# ---------------------------------------------------------------------------
# store_source
# ---------------------------------------------------------------------------


@respx.mock
async def test_store_source_happy_path(settings: Settings, tmp_path) -> None:
    pdf = tmp_path / "lab.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic lab bytes")
    import hashlib

    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    filename = f"copilot_{sha}.pdf"

    respx.get(f"{API}/patient/{PUUID}").mock(return_value=_patient_ok())
    listing = respx.get(f"{API}/patient/{PID}/document").mock(
        side_effect=[
            httpx.Response(404, text=""),  # dedup: empty folder
            httpx.Response(  # recovery after upload
                200,
                json=[
                    {
                        "filename": filename,
                        "hash": "sha3-512-value",
                        "id": 1849,
                        "mimetype": "application/pdf",
                        "docdate": "2026-07-13",
                    }
                ],
            ),
        ]
    )
    upload = respx.post(f"{API}/patient/{PID}/document").mock(
        return_value=httpx.Response(200, text="true")
    )

    async with _writer(settings) as w:
        ref = await w.store_source(PUUID, str(pdf), "lab_report")

    assert ref == SourceRef(resource_type="Document", id="1849")

    # Multipart field name is `document` (NOT `file`); path is a query param.
    req = upload.calls.last.request
    assert req.url.params.get("path") == "Lab_Report"
    body = req.content
    assert b'name="document"' in body
    assert b'name="file"' not in body
    assert sha.encode() in body  # deterministic SHA-256 filename embedded
    assert listing.call_count == 2


@respx.mock
async def test_store_source_is_idempotent(settings: Settings, tmp_path) -> None:
    pdf = tmp_path / "lab.pdf"
    pdf.write_bytes(b"%PDF-1.4 already stored")
    import hashlib

    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()

    respx.get(f"{API}/patient/{PUUID}").mock(return_value=_patient_ok())
    respx.get(f"{API}/patient/{PID}/document").mock(
        return_value=httpx.Response(
            200,
            json=[{"filename": f"copilot_{sha}.pdf", "hash": "h", "id": 1234}],
        )
    )
    upload = respx.post(f"{API}/patient/{PID}/document")

    async with _writer(settings) as w:
        ref = await w.store_source(PUUID, str(pdf), "lab_report")

    assert ref == SourceRef(resource_type="Document", id="1234")
    assert not upload.called  # dedup: same SHA-256 present → no re-upload


@respx.mock
async def test_store_source_missing_patient(settings: Settings, tmp_path) -> None:
    pdf = tmp_path / "lab.pdf"
    pdf.write_bytes(b"data")
    respx.get(f"{API}/patient/{PUUID}").mock(return_value=httpx.Response(404, text=""))

    async with _writer(settings) as w:
        with pytest.raises(OpenEmrWriteError) as excinfo:
            await w.store_source(PUUID, str(pdf), "lab_report")

    err = excinfo.value
    assert err.error.code == "patient_not_found"
    assert err.retriable is False


# ---------------------------------------------------------------------------
# persist_observations
# ---------------------------------------------------------------------------


def _citation(value: str) -> SourceCitation:
    return SourceCitation(
        source_type="lab_pdf",
        source_id="copilot_source.pdf",
        page_or_section="1",
        quote_or_value=value,
    )


def _observation(name: str, value: str, unit: str) -> LabObservation:
    return LabObservation(
        test_name=name,
        value=value,
        unit=unit,
        reference_range=None,
        collection_date=None,
        abnormal_flag="high",
        citation=_citation(f"{name} {value}"),
    )


def _report(*observations: LabObservation) -> LabReport:
    return LabReport(
        patient_ref=SourceRef(resource_type="Patient", id=PUUID),
        report_date=date(2026, 6, 30),
        observations=list(observations),
        extraction_confidence=0.9,
        source=_citation("lab report"),
    )


@respx.mock
async def test_persist_observations_happy_path(settings: Settings) -> None:
    report = _report(
        _observation("Glucose", "168", "mg/dL"),
        _observation("LDL Cholesterol", "155", "mg/dL"),
    )

    respx.get(f"{API}/patient/{PUUID}").mock(return_value=_patient_ok())
    respx.get(f"{API}/patient/{PUUID}/encounter").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    create_enc = respx.post(f"{API}/patient/{PUUID}/encounter").mock(
        return_value=httpx.Response(201, json={"data": {"eid": 1850, "encounter": 1850}})
    )
    respx.get(f"{API}/patient/{PID}/encounter/1850/vital").mock(
        return_value=httpx.Response(200, json=[])
    )
    vitals = respx.post(f"{API}/patient/{PID}/encounter/1850/vital").mock(
        side_effect=[
            httpx.Response(201, json={"vid": 26, "fid": 26}),
            httpx.Response(201, json={"vid": 27, "fid": 27}),
        ]
    )

    async with _writer(settings) as w:
        refs = await w.persist_observations(PUUID, report)

    assert refs == [
        SourceRef(resource_type="Encounter", id="1850"),
        SourceRef(resource_type="Vitals", id="26"),
        SourceRef(resource_type="Vitals", id="27"),
    ]
    assert create_enc.called
    assert vitals.call_count == 2
    # Encounter carries the deterministic ingestion marker for dedup.
    body = create_enc.calls.last.request.content
    assert b"Clinical Co-Pilot ingest:" in body


@respx.mock
async def test_persist_observations_is_idempotent(settings: Settings) -> None:
    report = _report(_observation("Glucose", "168", "mg/dL"))
    reason_note = "Glucose: 168 mg/dL"

    # Ingestion token is deterministic from the source id.
    from copilot.documents.openemr_write import _ingest_token

    reason = f"Clinical Co-Pilot ingest:{_ingest_token('copilot_source.pdf')}"

    respx.get(f"{API}/patient/{PUUID}").mock(return_value=_patient_ok())
    respx.get(f"{API}/patient/{PUUID}/encounter").mock(
        return_value=httpx.Response(
            200, json={"data": [{"eid": 1850, "reason": reason}]}
        )
    )
    create_enc = respx.post(f"{API}/patient/{PUUID}/encounter")
    respx.get(f"{API}/patient/{PID}/encounter/1850/vital").mock(
        return_value=httpx.Response(200, json=[{"id": 26, "note": reason_note}])
    )
    post_vital = respx.post(f"{API}/patient/{PID}/encounter/1850/vital")

    async with _writer(settings) as w:
        refs = await w.persist_observations(PUUID, report)

    assert refs == [
        SourceRef(resource_type="Encounter", id="1850"),
        SourceRef(resource_type="Vitals", id="26"),
    ]
    assert not create_enc.called  # existing ingestion encounter reused
    assert not post_vital.called  # existing vital note reused (no duplicate)


@respx.mock
async def test_persist_observations_partial_on_error(settings: Settings) -> None:
    report = _report(
        _observation("Glucose", "168", "mg/dL"),
        _observation("LDL Cholesterol", "155", "mg/dL"),
    )

    respx.get(f"{API}/patient/{PUUID}").mock(return_value=_patient_ok())
    respx.get(f"{API}/patient/{PUUID}/encounter").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.post(f"{API}/patient/{PUUID}/encounter").mock(
        return_value=httpx.Response(201, json={"data": {"eid": 1850}})
    )
    respx.get(f"{API}/patient/{PID}/encounter/1850/vital").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{API}/patient/{PID}/encounter/1850/vital").mock(
        side_effect=[
            httpx.Response(201, json={"vid": 26, "fid": 26}),
            httpx.Response(400, json={"validationErrors": ["bad"]}),
        ]
    )

    async with _writer(settings) as w:
        with pytest.raises(OpenEmrWriteError) as excinfo:
            await w.persist_observations(PUUID, report)

    err = excinfo.value
    assert err.partial is True
    assert err.error.code == "observations_partial"
    assert err.retriable is False  # 400 is permanent
    assert "LDL Cholesterol" in err.missing
    # The encounter and the first (successful) vital are not lost.
    assert SourceRef(resource_type="Encounter", id="1850") in err.partial_results
    assert SourceRef(resource_type="Vitals", id="26") in err.partial_results
