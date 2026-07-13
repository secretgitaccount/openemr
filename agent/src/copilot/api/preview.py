"""Read-only document-page preview for the click-to-source overlay (PRP-13, FR-7).

``POST /preview/page`` takes an uploaded document (the same bytes the UI already
holds in memory from its ``/documents`` upload) plus a 1-indexed ``page`` and
renders that page to a PNG so the browser can draw the bounding-box overlay. It
returns the image as a ``data:`` URI (so the page never fetches an external host)
together with the page's **pixel** size (the rendered raster) and its **point**
size (the PDF coordinate space the :class:`~copilot.documents.schemas.SourceCitation`
``bbox=`` locator is expressed in). With both, the UI scales a citation's box —
``bbox=x0,top,x1,bottom`` in points — onto the displayed image with a simple
fraction-of-page calculation, independent of how large the image is shown.

The endpoint is deliberately **stateless and non-persisting**: it reads the
uploaded bytes into memory, rasterizes with the same :mod:`pdfplumber`
``to_image`` path the extractor uses (PRP-04), and returns — nothing touches disk
and no PHI is stored server-side. Image-only documents (a scanned PNG with no
text layer, hence no word boxes) are reported with ``is_image=True`` and no point
dimensions, which is the UI's cue to degrade to a page-level highlight.
"""

from __future__ import annotations

import base64
import io

import pdfplumber
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from copilot.logging import get_logger

__all__ = ["router", "PagePreview"]

logger = get_logger(__name__)

router = APIRouter(tags=["preview"])

#: Rasterization density for the preview PNG. Independent of the citation math
#: (boxes are scaled by *fraction* of the page, not by absolute pixels), so this
#: only trades crispness against payload size.
_RENDER_DPI = 150

#: Guard against a pathological page index before touching the PDF.
_MAX_PAGE = 200

_PDF_SUFFIXES = {".pdf"}
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class PagePreview(BaseModel):
    """One rendered document page + the geometry the UI needs to place boxes.

    ``width_pt`` / ``height_pt`` are the PDF point dimensions the citation
    ``bbox=`` locator lives in (``None`` for an image-only document, which has no
    text-layer coordinate space). ``image_data_uri`` is a self-contained
    ``data:image/png;base64,...`` string so the page never requests an external
    host. Frozen + ``extra="forbid"`` like every other contract in the codebase.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(description="1-indexed page rendered (clamped into range).")
    page_count: int = Field(description="Total pages in the document.")
    is_image: bool = Field(
        description="True for an image-only doc (no text layer) — UI uses a page-level highlight.",
    )
    width_px: int = Field(description="Rendered raster width in pixels.")
    height_px: int = Field(description="Rendered raster height in pixels.")
    width_pt: float | None = Field(
        default=None,
        description="PDF page width in points (the bbox coordinate space); null for images.",
    )
    height_pt: float | None = Field(
        default=None,
        description="PDF page height in points (the bbox coordinate space); null for images.",
    )
    image_data_uri: str = Field(description="Self-contained data:image/png;base64 of the page.")


def _suffix(filename: str | None) -> str:
    name = filename or ""
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def _png_data_uri(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _render_pdf_page(contents: bytes, page: int) -> PagePreview:
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        page_count = len(pdf.pages)
        if page_count == 0:
            raise HTTPException(status_code=422, detail="document has no pages")
        index = max(1, min(page, page_count))
        pdf_page = pdf.pages[index - 1]
        width_pt = float(pdf_page.width)
        height_pt = float(pdf_page.height)
        raster = pdf_page.to_image(resolution=_RENDER_DPI).original
    return PagePreview(
        page=index,
        page_count=page_count,
        is_image=False,
        width_px=raster.width,
        height_px=raster.height,
        width_pt=width_pt,
        height_pt=height_pt,
        image_data_uri=_png_data_uri(raster),
    )


def _render_image(contents: bytes) -> PagePreview:
    with Image.open(io.BytesIO(contents)) as raw:
        raster = raw.convert("RGB")
    return PagePreview(
        page=1,
        page_count=1,
        is_image=True,
        width_px=raster.width,
        height_px=raster.height,
        width_pt=None,
        height_pt=None,
        image_data_uri=_png_data_uri(raster),
    )


@router.post("/preview/page")
async def preview_page(
    file: UploadFile = File(..., description="The source document to preview (PDF or image)."),
    page: int = Form(default=1, description="1-indexed page to render."),
) -> PagePreview:
    """Render one document page to a PNG for the click-to-source overlay.

    Reads the uploaded bytes in memory (nothing persisted), rasterizes the
    requested page, and returns the image plus its pixel and PDF-point
    dimensions so the UI can scale a citation's ``bbox=`` locator onto the
    displayed page. An unsupported file type is a 422; an out-of-range page is
    clamped into the document rather than erroring.
    """

    if page < 1 or page > _MAX_PAGE:
        raise HTTPException(status_code=422, detail=f"page out of range: {page}")

    suffix = _suffix(file.filename)
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=422, detail="empty upload")

    try:
        if suffix in _PDF_SUFFIXES:
            preview = _render_pdf_page(contents, page)
        elif suffix in _IMAGE_MEDIA_TYPES:
            preview = _render_image(contents)
        else:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported document type for preview: {suffix!r}",
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — render failures become a clean 422, never a 500 leak
        logger.warning("preview.render_failed", suffix=suffix)
        raise HTTPException(status_code=422, detail="could not render this document") from exc

    logger.info(
        "preview.ok",
        suffix=suffix,
        page=preview.page,
        page_count=preview.page_count,
        is_image=preview.is_image,
    )
    return preview
