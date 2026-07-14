"""Ingest documents already in the chart — read what OTHERS uploaded (PRP-15).

In a real clinic the physician does **not** upload PDFs: the front desk, a
nurse, or the patient portal scans a lab report or intake form into OpenEMR's
Documents module, and the doctor wants it *read*. This module is the co-pilot's
"ingest from the chart" path — it reads a document **already present** in
OpenEMR (never one the doctor uploaded through our tool) and turns it into the
same schema-validated, per-value-cited facts the upload path (PRP-06) produces.

Read paths (FHIR, verified against the local ``development-easy`` stack)
------------------------------------------------------------------------
* **List** — ``GET {fhir}/DocumentReference?patient=<patientUUID>``. Each
  ``content[].attachment.url`` is ``.../Binary/<id>``; that ``<id>`` is the
  stable document id we key everything on.
* **Bytes** — ``GET {fhir}/Binary/<id>`` returns the **decrypted raw bytes**
  (OpenEMR streams the file when the request does not force ``fhir+json``; when
  it answers with a FHIR ``Binary`` resource we decode its base64 ``data``).

Scopes: ``user/DocumentReference.read`` + ``user/Binary.read`` (on top of the
Week-1 FHIR read scopes) — see :data:`CHART_READ_SCOPES`.

Grounding: the source of an ingested-from-chart fact is the **existing** chart
document, so this path does **not** re-upload / ``store_source`` a copy. The
returned :class:`~copilot.documents.ingest.IngestResult` points its ``source``
at that existing document, and every extraction
:class:`~copilot.documents.schemas.SourceCitation` is relabelled so its
``source_id`` is the OpenEMR document id (stable) — the UI maps a citation back
to the chart document by that id, fixing the temp-basename linkage the upload
path used. Derived lab values are still persisted idempotently via PRP-05
:func:`~copilot.documents.openemr_write.persist_observations`.

A single ``correlation_id`` is bound for the whole ingest so the fetch / extract
/ persist steps share one trace; no PHI is logged (only ids, counts, doc type).
"""

from __future__ import annotations

import base64
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from copilot.config import Settings, get_settings
from copilot.documents.extract import VLMExtractor, extract_intake, extract_lab
from copilot.documents.extract_cache import extract_cache
from copilot.documents.ingest import DOC_TYPES, IngestError, IngestResult
from copilot.documents.openemr_write import OpenEmrWriter, persist_observations
from copilot.documents.schemas import (
    CitedList,
    CitedText,
    IntakeDemographics,
    IntakeFacts,
    LabReport,
    SourceCitation,
)
from copilot.logging import (
    CORRELATION_ID_HEADER,
    current_correlation_id,
    get_logger,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from copilot.openemr.client import TokenSource
from copilot.openemr.oauth import (
    ClientCredentials,
    OAuthError,
    TokenResponse,
    default_scope_string,
    register_client,
    request_password_token,
    request_refresh_token,
)
from copilot.schemas.core import SourceRef

__all__ = [
    "CHART_READ_SCOPES",
    "ChartDocument",
    "ChartReadError",
    "ChartReadTokenProvider",
    "ChartReader",
    "list_chart_documents",
    "fetch_document_bytes",
    "ingest_chart_document",
    "extract_chart_document",
]

logger = get_logger(__name__)

_HTTP_TIMEOUT_SECONDS = 30.0

#: The FHIR read scopes this path needs: the Week-1 provider-context read
#: surface **plus** the two document-read scopes. ``user/Binary.read`` streams
#: the decrypted file; ``user/DocumentReference.read`` lists the chart's docs.
CHART_READ_SCOPES = (
    f"{default_scope_string()} user/DocumentReference.read user/Binary.read"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChartReadError(RuntimeError):
    """A read of the chart's documents failed (list or Binary fetch).

    ``retriable`` marks a transient failure (network blip, 5xx, 429) worth a
    retry versus a permanent one (404, 401, malformed body). Messages never
    contain document bytes or response bodies (no PHI) — only status + operation.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class ChartDocument(BaseModel):
    """One document already in a patient's chart (a FHIR ``DocumentReference``).

    ``doc_id`` is the stable OpenEMR id taken from the ``Binary/<id>`` attachment
    URL — the id :func:`fetch_document_bytes` / :func:`ingest_chart_document`
    key on and the id every extracted citation is grounded to. Frozen +
    ``extra="forbid"`` like every other contract in the codebase.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str = Field(min_length=1, description="Stable OpenEMR document (Binary) id.")
    title: str = Field(min_length=1, description="Human-readable document title.")
    category: str | None = Field(default=None, description="Document category, if classified.")
    date: str | None = Field(default=None, description="Document date (FHIR date string), if known.")
    mimetype: str | None = Field(default=None, description="Attachment content type, if known.")


# ---------------------------------------------------------------------------
# FHIR-body helpers (guarded, PHI-agnostic)
# ---------------------------------------------------------------------------


def _bundle_resources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the ``resource`` dicts from a FHIR search Bundle, guarded."""

    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return []
    resources: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict):
            resource = entry.get("resource")
            if isinstance(resource, dict):
                resources.append(resource)
    return resources


def _binary_id_from_url(url: Any) -> str | None:
    """Extract ``<id>`` from a ``.../Binary/<id>`` attachment URL."""

    if not isinstance(url, str) or "Binary/" not in url:
        return None
    tail = url.rsplit("Binary/", 1)[1].strip("/")
    return tail or None


def _first(seq: Any) -> dict[str, Any] | None:
    if isinstance(seq, list):
        for item in seq:
            if isinstance(item, dict):
                return item
    return None


def _coding_display(node: Any) -> str | None:
    """Read a CodeableConcept's ``text`` or first ``coding[].display``."""

    if not isinstance(node, dict):
        return None
    text = node.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    coding = _first(node.get("coding"))
    if coding is not None:
        display = coding.get("display") or coding.get("code")
        if isinstance(display, str) and display.strip():
            return display.strip()
    return None


def _map_document(resource: dict[str, Any]) -> ChartDocument | None:
    """Map a FHIR ``DocumentReference`` to a :class:`ChartDocument`, or skip it.

    A resource without a resolvable ``Binary/<id>`` is skipped rather than
    surfaced — we can only ingest a document whose bytes we can fetch.
    """

    content = _first(resource.get("content"))
    attachment = content.get("attachment") if content else None
    attachment = attachment if isinstance(attachment, dict) else {}
    doc_id = _binary_id_from_url(attachment.get("url"))
    if doc_id is None:
        return None

    title = (
        (attachment.get("title") if isinstance(attachment.get("title"), str) else None)
        or (resource.get("description") if isinstance(resource.get("description"), str) else None)
        or _coding_display(resource.get("type"))
        or _coding_display(_first(resource.get("category")))
        or f"Document {doc_id}"
    )
    category = _coding_display(_first(resource.get("category"))) or _coding_display(
        resource.get("type")
    )
    date = resource.get("date") or attachment.get("creation")
    mimetype = attachment.get("contentType")
    return ChartDocument(
        doc_id=doc_id,
        title=str(title).strip() or f"Document {doc_id}",
        category=category,
        date=str(date) if isinstance(date, str) and date else None,
        mimetype=str(mimetype) if isinstance(mimetype, str) and mimetype else None,
    )


def _decode_binary_body(resp: httpx.Response) -> bytes:
    """Return the file bytes from a Binary response (raw stream or FHIR JSON)."""

    content_type = resp.headers.get("content-type", "")
    if "json" in content_type.lower():
        try:
            body = resp.json()
        except ValueError:
            return resp.content
        if isinstance(body, dict) and isinstance(body.get("data"), str):
            try:
                return base64.b64decode(body["data"])
            except (ValueError, TypeError):
                return resp.content
    return resp.content


# ---------------------------------------------------------------------------
# Chart reader (FHIR DocumentReference + Binary)
# ---------------------------------------------------------------------------


class ChartReader:
    """Reads a patient's chart documents over OpenEMR FHIR.

    Authenticates as the user (the ``token_source`` vends a bearer token with
    ``DocumentReference.read`` + ``Binary.read``), stamps ``X-Correlation-ID`` on
    every call, and returns typed :class:`ChartDocument` metadata or the raw
    decrypted document bytes. ``client`` may be injected (tests wire an httpx
    client to a mock transport); otherwise one is created lazily and owned here.
    """

    def __init__(
        self,
        token_source: TokenSource,
        *,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._tokens = token_source
        self._settings = settings or get_settings()
        self._base = self._settings.openemr_fhir_base.rstrip("/")
        self._client = client
        self._owns_client = client is None

    # -- lifecycle ---------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_SECONDS,
                verify=_tls_verify(self._base),
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> ChartReader:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- requests ----------------------------------------------------------

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._tokens.get_access_token()}",
            "Accept": accept,
        }
        cid = current_correlation_id()
        if cid:
            headers[CORRELATION_ID_HEADER] = cid
        return headers

    async def _get(self, path: str, *, accept: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            resp = await self._http().get(url, params=params, headers=self._headers(accept))
        except httpx.HTTPError as exc:
            logger.warning("chart_read.transport_error", op=path.split("/", 1)[0])
            raise ChartReadError("chart read failed to connect", retriable=True) from exc
        if resp.status_code >= 400:
            retriable = resp.status_code in {429, 500, 502, 503, 504}
            logger.warning(
                "chart_read.http_error", status=resp.status_code, retriable=retriable
            )
            raise ChartReadError(
                f"chart read returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                retriable=retriable,
            )
        return resp

    async def list_chart_documents(self, patient_id: str) -> list[ChartDocument]:
        """List the patient's chart documents (front-desk / nurse / portal uploads)."""

        resp = await self._get(
            "DocumentReference",
            accept="application/fhir+json",
            params={"patient": patient_id},
        )
        try:
            bundle = resp.json()
        except ValueError as exc:
            raise ChartReadError("DocumentReference response was not valid JSON") from exc
        if not isinstance(bundle, dict):
            raise ChartReadError("DocumentReference response was not a JSON object")
        docs = [
            doc
            for resource in _bundle_resources(bundle)
            if (doc := _map_document(resource)) is not None
        ]
        logger.info("chart_read.list.ok", count=len(docs))
        return docs

    async def fetch_document_bytes(self, patient_id: str, doc_id: str) -> bytes:
        """Fetch one chart document's decrypted raw bytes via FHIR ``Binary``."""

        resp = await self._get(
            f"Binary/{doc_id}",
            accept="application/octet-stream, application/fhir+json;q=0.5",
        )
        data = _decode_binary_body(resp)
        logger.info("chart_read.fetch.ok", doc_id=doc_id, bytes=len(data))
        return data


def _tls_verify(url: str) -> bool:
    if not url.startswith("https://"):
        return True
    return not (url.startswith("https://localhost") or url.startswith("https://127.0.0.1"))


# ---------------------------------------------------------------------------
# Read token provider (FHIR audience, chart-read scopes incl. the two new ones)
# ---------------------------------------------------------------------------


class ChartReadTokenProvider:
    """Caches a user-bound token pinned to :data:`CHART_READ_SCOPES`.

    Mirrors the Week-1 ``TokenProvider`` but pins the scope string so the token
    actually carries ``DocumentReference.read`` + ``Binary.read`` (the plain
    provider requests only the default read scopes). Used only on the live path;
    unit tests inject a stub token source into :class:`ChartReader`.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        credentials: ClientCredentials,
        scopes: str = CHART_READ_SCOPES,
        settings: Settings | None = None,
        refresh_skew_seconds: float = 60.0,
    ) -> None:
        self._username = username
        self._password = password
        self._credentials = credentials
        self._scopes = tuple(scopes.split())
        self._settings = settings or get_settings()
        self._skew = refresh_skew_seconds
        self._token: TokenResponse | None = None
        self._expires_at_monotonic: float = 0.0

    def get_access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._expires_at_monotonic:
            return self._token.access_token

        if self._token is not None and self._token.refresh_token:
            try:
                self._store(
                    request_refresh_token(
                        self._token.refresh_token,
                        credentials=self._credentials,
                        settings=self._settings,
                    )
                )
                return self._token.access_token
            except OAuthError:
                logger.warning("chart_read.refresh_failed_reauth")

        self._store(
            request_password_token(
                self._username,
                self._password,
                credentials=self._credentials,
                settings=self._settings,
                scopes=self._scopes,
            )
        )
        return self._token.access_token  # type: ignore[union-attr]

    def _store(self, token: TokenResponse) -> None:
        self._token = token
        self._expires_at_monotonic = time.monotonic() + token.expires_in - self._skew


def _default_reader() -> ChartReader:
    """Wire a live chart reader: read client credentials + chart-read scopes."""

    settings = get_settings()
    creds = register_client(settings=settings, scopes=tuple(CHART_READ_SCOPES.split()))
    provider = ChartReadTokenProvider(
        settings.openemr_dev_user,
        settings.openemr_dev_pass,
        credentials=creds,
        scopes=CHART_READ_SCOPES,
        settings=settings,
    )
    return ChartReader(provider, settings=settings)


# ---------------------------------------------------------------------------
# Citation relabelling: ground every extracted fact to the OpenEMR doc id
# ---------------------------------------------------------------------------


def _relabel(citation: SourceCitation, doc_id: str) -> SourceCitation:
    return citation.model_copy(update={"source_id": doc_id})


def _relabel_lab(report: LabReport, doc_id: str) -> LabReport:
    """Return a copy of ``report`` with every citation grounded to ``doc_id``."""

    observations = [
        obs.model_copy(update={"citation": _relabel(obs.citation, doc_id)})
        for obs in report.observations
    ]
    return report.model_copy(
        update={
            "observations": observations,
            "source": _relabel(report.source, doc_id),
        }
    )


def _relabel_intake(facts: IntakeFacts, doc_id: str) -> IntakeFacts:
    """Return a copy of ``facts`` with every citation grounded to ``doc_id``."""

    demographics: IntakeDemographics = facts.demographics.model_copy(
        update={"citation": _relabel(facts.demographics.citation, doc_id)}
    )
    chief_concern: CitedText | None = facts.chief_concern
    if chief_concern is not None:
        chief_concern = chief_concern.model_copy(
            update={"citation": _relabel(chief_concern.citation, doc_id)}
        )
    medications: CitedList = facts.current_medications.model_copy(
        update={"citation": _relabel(facts.current_medications.citation, doc_id)}
    )
    allergies: CitedList = facts.allergies.model_copy(
        update={"citation": _relabel(facts.allergies.citation, doc_id)}
    )
    family_history: CitedList = facts.family_history.model_copy(
        update={"citation": _relabel(facts.family_history.citation, doc_id)}
    )
    return facts.model_copy(
        update={
            "demographics": demographics,
            "chief_concern": chief_concern,
            "current_medications": medications,
            "allergies": allergies,
            "family_history": family_history,
            "source": _relabel(facts.source, doc_id),
        }
    )


def _relabel_extracted(
    extracted: LabReport | IntakeFacts, doc_id: str
) -> LabReport | IntakeFacts:
    if isinstance(extracted, LabReport):
        return _relabel_lab(extracted, doc_id)
    return _relabel_intake(extracted, doc_id)


# ---------------------------------------------------------------------------
# Byte sniffing for the temp-file suffix (the extractor renders by suffix)
# ---------------------------------------------------------------------------


def _sniff_suffix(data: bytes, mimetype: str | None) -> str:
    """Pick a file suffix so the reused extractor renders the right way."""

    if data[:5] == b"%PDF-":
        return ".pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if mimetype:
        mt = mimetype.lower()
        if "pdf" in mt:
            return ".pdf"
        if "png" in mt:
            return ".png"
        if "jpeg" in mt or "jpg" in mt:
            return ".jpg"
    # Default to PDF — the dominant chart-document format for labs/intake.
    return ".pdf"


# ---------------------------------------------------------------------------
# Module-level API (the PRP-15 contract)
# ---------------------------------------------------------------------------


async def list_chart_documents(
    patient_id: str, *, reader: ChartReader | None = None
) -> list[ChartDocument]:
    """List the documents already in a patient's chart (see :class:`ChartReader`)."""

    if reader is not None:
        return await reader.list_chart_documents(patient_id)
    async with _default_reader() as r:
        return await r.list_chart_documents(patient_id)


async def fetch_document_bytes(
    patient_id: str, doc_id: str, *, reader: ChartReader | None = None
) -> bytes:
    """Fetch one chart document's decrypted raw bytes (see :class:`ChartReader`)."""

    if reader is not None:
        return await reader.fetch_document_bytes(patient_id, doc_id)
    async with _default_reader() as r:
        return await r.fetch_document_bytes(patient_id, doc_id)


async def _extract_bytes(
    file_path: Path, doc_type: str, extractor: VLMExtractor | None
) -> LabReport | IntakeFacts:
    """Run the doc-type-appropriate extractor (an injected one wins, for tests)."""

    if doc_type == "lab_pdf":
        if extractor is not None:
            return await extractor.extract_lab(file_path)
        return await extract_lab(file_path)
    # intake_form (the only other member of DOC_TYPES)
    if extractor is not None:
        return await extractor.extract_intake(file_path)
    return await extract_intake(file_path)


async def _fetch_and_extract(
    patient_id: str,
    doc_id: str,
    doc_type: str,
    *,
    reader: ChartReader | None,
    extractor: VLMExtractor | None,
) -> LabReport | IntakeFacts:
    """Fetch the chart document's bytes, extract, and ground citations to ``doc_id``.

    The shared read-side kernel of both :func:`ingest_chart_document` (read +
    persist) and :func:`extract_chart_document` (extract-only): it never persists
    or populates the cache — its callers own those steps.
    """

    data = await fetch_document_bytes(patient_id, doc_id, reader=reader)
    suffix = _sniff_suffix(data, None)
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.flush()
        tmp.close()
        extracted = await _extract_bytes(Path(tmp.name), doc_type, extractor)
    finally:
        os.unlink(tmp.name)

    # Ground every citation (and the report's own source) to the stable OpenEMR
    # document id so the UI maps citation -> chart document.
    return _relabel_extracted(extracted, doc_id)


async def extract_chart_document(
    patient_id: str,
    doc_id: str,
    doc_type: str,
    *,
    reader: ChartReader | None = None,
    extractor: VLMExtractor | None = None,
) -> LabReport | IntakeFacts:
    """Extract a chart document's facts — **cache-first, no re-persist** (PRP-17).

    The read path for ``/ask`` grounding: when the doctor has already **read** the
    document (:func:`ingest_chart_document` warmed :data:`extract_cache`), this
    returns that stored extraction and **skips the second VLM call** entirely; on
    a cold cache it fetches + extracts once (grounded to ``doc_id``) and warms the
    cache. Unlike :func:`ingest_chart_document` it never re-persists / writes
    OpenEMR records — it only reads and extracts, so an ``/ask`` can never leave a
    stray persisted record. ``doc_type`` must be one of
    :data:`~copilot.documents.ingest.DOC_TYPES` (else :class:`IngestError`).
    """

    doc_type = (doc_type or "").strip()
    if doc_type not in DOC_TYPES:
        raise IngestError(
            f"unknown doc_type {doc_type!r}; expected one of {sorted(DOC_TYPES)}"
        )

    cached = extract_cache.get(patient_id, doc_id)
    if cached is not None:
        logger.info("chart_extract.cache_hit", doc_type=doc_type, doc_id=doc_id)
        return cached

    token = None if current_correlation_id() else set_correlation_id(new_correlation_id())
    try:
        logger.info("chart_extract.start", doc_type=doc_type, doc_id=doc_id)
        extracted = await _fetch_and_extract(
            patient_id, doc_id, doc_type, reader=reader, extractor=extractor
        )
        extract_cache.put(patient_id, doc_id, extracted)
        logger.info("chart_extract.ok", doc_type=doc_type, doc_id=doc_id)
        return extracted
    finally:
        if token is not None:
            reset_correlation_id(token)


async def ingest_chart_document(
    patient_id: str,
    doc_id: str,
    doc_type: str,
    *,
    reader: ChartReader | None = None,
    extractor: VLMExtractor | None = None,
    writer: OpenEmrWriter | None = None,
) -> IngestResult:
    """Ingest a document **already in the chart** (fetch → extract → persist).

    Unlike the upload path (PRP-06), the source is the *existing* chart document,
    so nothing is re-uploaded: the returned :class:`IngestResult` points its
    ``source`` at that document and every extraction citation is relabelled so
    its ``source_id`` is ``doc_id`` (stable), letting the UI map a citation back
    to the chart document. Derived lab values are still persisted idempotently
    (PRP-05). The extraction is also written to :data:`extract_cache` so a
    subsequent ``/ask`` grounded on the same document reuses it without a second
    VLM call (PRP-17). ``doc_type`` must be one of
    :data:`~copilot.documents.ingest.DOC_TYPES` (else :class:`IngestError`).
    ``reader`` / ``extractor`` / ``writer`` may be injected for testing.
    """

    doc_type = (doc_type or "").strip()
    if doc_type not in DOC_TYPES:
        raise IngestError(
            f"unknown doc_type {doc_type!r}; expected one of {sorted(DOC_TYPES)}"
        )

    token = None if current_correlation_id() else set_correlation_id(new_correlation_id())
    try:
        logger.info("chart_ingest.start", doc_type=doc_type, doc_id=doc_id)

        extracted = await _fetch_and_extract(
            patient_id, doc_id, doc_type, reader=reader, extractor=extractor
        )

        # Warm the extraction cache so the doctor's "Read" makes the subsequent
        # "Ask" (extract_chart_document) reuse this without a second VLM call.
        extract_cache.put(patient_id, doc_id, extracted)

        # The source is the EXISTING chart document — do NOT re-upload it.
        source = SourceRef(resource_type="Document", id=doc_id)

        # Derived lab values are still persisted (idempotent, keyed by doc_id via
        # the relabelled report.source); intake forms have no derived write path.
        record_refs: list[SourceRef] = []
        if isinstance(extracted, LabReport):
            record_refs = await persist_observations(patient_id, extracted, writer=writer)

        result = IngestResult(
            source=source,
            extracted=extracted,
            record_refs=record_refs,
            confidence=extracted.extraction_confidence,
        )
        logger.info(
            "chart_ingest.ok",
            doc_type=doc_type,
            doc_id=doc_id,
            record_refs=len(record_refs),
        )
        return result
    finally:
        if token is not None:
            reset_correlation_id(token)
