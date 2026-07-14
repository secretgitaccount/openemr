"""Read-from-chart endpoints — ingest what the front desk / nurse / portal uploaded (PRP-15).

The physician does not upload PDFs through the co-pilot; the documents are
already in OpenEMR's chart. This router exposes that read path:

* ``GET  /patients/{patient_id}/chart-documents`` — list the chart's documents
  (FHIR ``DocumentReference``) as :class:`~copilot.documents.chart_read.ChartDocument`.
* ``POST /patients/{patient_id}/chart-documents/{doc_id}/ingest?doc_type=…`` —
  fetch the document's bytes (FHIR ``Binary``), extract schema-validated facts,
  persist derived lab values, and return the strict
  :class:`~copilot.documents.ingest.IngestResult` (its ``source`` points at the
  existing document; every citation is grounded to ``doc_id``). An unknown
  ``doc_type`` is a **422** before any work.
* ``GET  /patients/{patient_id}/chart-documents/{doc_id}/page/{n}`` — render one
  page of the chart document to a PNG data-URI + dims (reusing the PRP-13
  ``preview`` renderer) so the UI can draw the click-to-source bounding boxes.

The network wiring (OAuth read client → user-bound token → FHIR reads) lives
behind the :func:`get_chart_reader` / :func:`get_chart_ingestor` dependencies so
tests override them wholesale (stub token + mocked FHIR + stubbed VLM) without a
key or a live stack — the same override seam the other routers use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from copilot.api.preview import PagePreview, _render_image, _render_pdf_page
from copilot.config import get_settings
from copilot.documents.chart_read import (
    CHART_READ_SCOPES,
    ChartDocument,
    ChartReadError,
    ChartReadTokenProvider,
    ChartReader,
    ingest_chart_document,
)
from copilot.documents.ingest import DOC_TYPES, IngestError, IngestResult
from copilot.documents.openemr_write import OpenEmrWriteError
from copilot.logging import get_logger
from copilot.openemr.oauth import register_client
from copilot.openemr.smart import StaticTokenSource
from copilot.smart_session import SESSION_COOKIE, get_session

__all__ = ["router", "get_chart_reader", "get_chart_ingestor", "ChartIngestor"]

logger = get_logger(__name__)

router = APIRouter(tags=["chart-documents"])

#: Guard against a pathological page index before touching the document.
_MAX_PAGE = 200

_PDF_MAGIC = b"%PDF-"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


#: The chart-ingest tool's call signature (patient_id, doc_id, doc_type) ->
#: IngestResult. Behind a dependency so tests substitute a version wired to a
#: stubbed VLM + mocked OpenEMR.
ChartIngestor = Callable[[str, str, str], Awaitable[IngestResult]]


# ---------------------------------------------------------------------------
# Dependencies — network-wired reader / ingestor (overridden in tests)
# ---------------------------------------------------------------------------


async def get_chart_reader(request: Request) -> AsyncIterator[ChartReader]:
    """Yield a fully-wired :class:`ChartReader` for the life of one request.

    Identity is resolved per request (mirroring the summary endpoint): a SMART
    launch session borrows the clinician's token; otherwise the dev read client
    is used with a **chart-read**-scoped password grant (so the token carries
    ``DocumentReference.read`` + ``Binary.read``). Tests override this dependency
    with a reader wired to a stub token + mocked FHIR, so no OAuth / live stack
    is touched in unit tests.
    """

    settings = get_settings()
    session = get_session(request.cookies.get(SESSION_COOKIE))
    if session is not None:
        token_source: object = StaticTokenSource(session.access_token)
    else:
        creds = register_client(settings=settings, scopes=tuple(CHART_READ_SCOPES.split()))
        token_source = ChartReadTokenProvider(
            settings.openemr_dev_user,
            settings.openemr_dev_pass,
            credentials=creds,
            settings=settings,
        )
    async with ChartReader(token_source, settings=settings) as reader:  # type: ignore[arg-type]
        yield reader


def get_chart_ingestor() -> ChartIngestor:
    """Yield the chart-ingest tool (overridden in tests with a mocked composition)."""

    return ingest_chart_document


# ---------------------------------------------------------------------------
# Byte-sniffing for the page renderer
# ---------------------------------------------------------------------------


def _render_page(contents: bytes, page: int) -> PagePreview:
    """Render one chart-document page reusing the PRP-13 preview renderer."""

    if contents[:5] == _PDF_MAGIC:
        return _render_pdf_page(contents, page)
    if contents[:8] == _PNG_MAGIC or contents[:3] == _JPEG_MAGIC:
        return _render_image(contents)
    # Fall back to a PDF render attempt; a genuine non-document raises below.
    return _render_pdf_page(contents, page)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/patients/{patient_id}/chart-documents")
async def list_chart_documents_endpoint(
    patient_id: str,
    reader: ChartReader = Depends(get_chart_reader),
) -> list[ChartDocument]:
    """List the documents already in the patient's chart (front-desk / nurse / portal)."""

    try:
        documents = await reader.list_chart_documents(patient_id)
    except ChartReadError as exc:
        status = 502 if exc.retriable else (exc.status_code or 502)
        logger.warning("chart_documents.list.read_error", status=exc.status_code)
        raise HTTPException(status_code=status, detail="could not list chart documents") from exc
    logger.info("chart_documents.list.ok", count=len(documents))
    return documents


@router.post("/patients/{patient_id}/chart-documents/{doc_id}/ingest")
async def ingest_chart_document_endpoint(
    patient_id: str,
    doc_id: str,
    doc_type: str = Query(..., description="One of: lab_pdf, intake_form."),
    ingestor: ChartIngestor = Depends(get_chart_ingestor),
) -> JSONResponse:
    """Ingest a document already in the chart and return the cited result.

    Rejects an unknown ``doc_type`` with 422 (before any work). On success
    returns the :class:`IngestResult` JSON: the existing-document source ref, the
    validated extracted facts with per-value citations grounded to ``doc_id``,
    the persisted record refs, and the extraction confidence.
    """

    if doc_type not in DOC_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown doc_type {doc_type!r}; expected one of {sorted(DOC_TYPES)}",
        )

    try:
        result = await ingestor(patient_id, doc_id, doc_type)
    except IngestError as exc:  # defensive: bad doc_type reaching the tool
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ChartReadError as exc:
        status = 502 if exc.retriable else (exc.status_code or 502)
        logger.warning("chart_documents.ingest.read_error", status=exc.status_code)
        raise HTTPException(status_code=status, detail="could not read chart document") from exc
    except OpenEmrWriteError as exc:
        status = 502 if exc.retriable else 400
        logger.warning("chart_documents.ingest.write_error", code=exc.error.code)
        raise HTTPException(status_code=status, detail=exc.error.message) from exc

    return JSONResponse(content=result.model_dump(mode="json"))


@router.get("/patients/{patient_id}/chart-documents/{doc_id}/page/{n}")
async def chart_document_page_endpoint(
    patient_id: str,
    doc_id: str,
    n: int,
    reader: ChartReader = Depends(get_chart_reader),
) -> PagePreview:
    """Render one page of a chart document to a PNG data-URI + dims for the overlay.

    Fetches the document's bytes from OpenEMR and rasterizes page ``n`` with the
    same renderer the upload preview uses (PRP-13), so the UI scales a citation's
    ``bbox=`` locator onto the displayed page. An out-of-range page is clamped;
    an unrenderable document is a 422.
    """

    if n < 1 or n > _MAX_PAGE:
        raise HTTPException(status_code=422, detail=f"page out of range: {n}")

    try:
        contents = await reader.fetch_document_bytes(patient_id, doc_id)
    except ChartReadError as exc:
        status = 502 if exc.retriable else (exc.status_code or 502)
        logger.warning("chart_documents.page.read_error", status=exc.status_code)
        raise HTTPException(status_code=status, detail="could not fetch chart document") from exc

    if not contents:
        raise HTTPException(status_code=422, detail="chart document is empty")

    try:
        preview = _render_page(contents, n)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — render failures become a clean 422, never a 500 leak
        logger.warning("chart_documents.page.render_failed", doc_id=doc_id)
        raise HTTPException(status_code=422, detail="could not render this document") from exc

    logger.info(
        "chart_documents.page.ok",
        doc_id=doc_id,
        page=preview.page,
        page_count=preview.page_count,
        is_image=preview.is_image,
    )
    return preview
