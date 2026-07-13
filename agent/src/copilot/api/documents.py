"""The FR-1 document ingestion endpoint (PRP-06).

``POST /patients/{patient_id}/documents`` accepts a multipart file upload plus a
``doc_type`` form field, runs :func:`~copilot.documents.ingest.attach_and_extract`
(store source → extract → persist), and returns the strict
:class:`~copilot.documents.ingest.IngestResult` as JSON — the source ref, the
schema-validated extracted facts (per-value citations included), the persisted
OpenEMR record refs, and the extraction confidence.

An unknown ``doc_type`` is a client error and returns **422** before any VLM or
network work happens. The composed tool lives behind the :func:`get_ingestor`
dependency so tests override it wholesale (stubbed VLM + mocked OpenEMR) without
a key or a live stack — the same override seam used by the summary/chat routers.

The upload bytes are written to a private temp file (the extractor reads a real
path for pdfplumber rendering + word boxes) and unlinked in a ``finally`` so
nothing lingers on disk after the request.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from copilot.documents.ingest import DOC_TYPES, IngestError, IngestResult, attach_and_extract
from copilot.documents.openemr_write import OpenEmrWriteError
from copilot.logging import get_logger

__all__ = ["router", "get_ingestor", "Ingestor"]

logger = get_logger(__name__)

router = APIRouter(tags=["documents"])

#: The composed ingestion tool's call signature (patient_id, file_path,
#: doc_type) -> IngestResult. Behind a dependency so tests can substitute a
#: version wired to a stubbed VLM + mocked OpenEMR.
Ingestor = Callable[[str, str, str], Awaitable[IngestResult]]


def get_ingestor() -> Ingestor:
    """Yield the ingestion tool (overridden in tests with a mocked composition)."""

    return attach_and_extract


@router.post("/patients/{patient_id}/documents")
async def ingest_document_endpoint(
    patient_id: str,
    file: UploadFile = File(..., description="The source document (lab PDF / intake form)."),
    doc_type: str = Form(..., description="One of: lab_pdf, intake_form."),
    ingestor: Ingestor = Depends(get_ingestor),
) -> JSONResponse:
    """Ingest one uploaded clinical document and return the cited result.

    Rejects an unknown ``doc_type`` with 422 (before any work). On success
    returns the :class:`IngestResult` JSON: the stored source ref, the validated
    extracted facts with per-value citations, the persisted record refs, and the
    extraction confidence.
    """

    if doc_type not in DOC_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown doc_type {doc_type!r}; expected one of {sorted(DOC_TYPES)}",
        )

    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    contents = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(contents)
        tmp.flush()
        tmp.close()
        try:
            result = await ingestor(patient_id, tmp.name, doc_type)
        except IngestError as exc:  # defensive: bad doc_type reaching the tool
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OpenEmrWriteError as exc:
            status = 502 if exc.retriable else 400
            logger.warning(
                "documents.ingest.write_error",
                code=exc.error.code,
                status=exc.status_code,
            )
            raise HTTPException(status_code=status, detail=exc.error.message) from exc
    finally:
        os.unlink(tmp.name)

    return JSONResponse(content=result.model_dump(mode="json"))
