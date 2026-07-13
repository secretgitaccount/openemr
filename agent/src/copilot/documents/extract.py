"""VLM document extraction — schema-bound, box-grounded (PRP-04, FR-6).

Turn a lab PDF or intake form into a validated
:class:`~copilot.documents.schemas.LabReport` /
:class:`~copilot.documents.schemas.IntakeFacts` via Claude vision — where **the
schema, not the model, is the source of truth**.

Design (why there are two Pydantic gates)
-----------------------------------------
The VLM reads *page images* and can only report what it sees: analyte names,
values, the verbatim text it read each value from, and a self-assessed
confidence. It cannot know the FHIR ``patient_ref`` (that link is resolved
downstream in PRP-06) nor the exact pixel/point bounding box of a value (that
comes from :mod:`pdfplumber`'s word boxes, PRP-03). So the raw VLM call is bound
with Anthropic structured output (``messages.parse``) to a strict *extraction*
schema (:class:`LabExtraction` / :class:`IntakeExtraction`) — raw output that
does not satisfy it is rejected/retried at the tool layer, never surfaced.

We then **assemble** the canonical PRP-02 contract from that validated
extraction: each value's verbatim quote is matched against the page's word
boxes to attach a :class:`SourceCitation` (page + bounding box in
``field_or_chunk_id`` + quoted value). Constructing the frozen ``LabReport`` /
``IntakeFacts`` is the second validation gate. Nothing the VLM emits reaches a
caller without passing through both.

Grounding & honesty rules:

* Every extracted value carries a ``SourceCitation``. When a value's quote maps
  to a word box, the citation is box-level (``field_or_chunk_id`` holds the
  box); when no box exists (an image-only scan with no text layer, or a quote
  that could not be located), the citation degrades to **page-level**
  (``page_or_section`` set, ``field_or_chunk_id`` ``None``) rather than being
  dropped.
* Unreadable / low-confidence fields are emitted as ``None`` by the VLM (the
  schema requires an explicit ``null``); this module never invents a value.
* ``extraction_confidence`` is the VLM's self-report **clamped** by the
  fraction of extracted values that were actually box-grounded.

The vision model defaults to Claude Opus 4.8 and is configurable via the
``COPILOT_VLM_MODEL`` environment variable or the ``model`` constructor arg. The
Anthropic client is constructed lazily, so importing this module never needs a
key; the "no key configured" error only fires when a live call is attempted.
The VLM call is wrapped with a tenacity retry (transient failures only) and a
per-request timeout.
"""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber
import pydantic
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from copilot.config import Settings, get_settings
from copilot.documents.schemas import (
    AbnormalFlag,
    CitedList,
    CitedText,
    IntakeDemographics,
    IntakeFacts,
    LabObservation,
    LabReport,
    SourceCitation,
)
from copilot.logging import CORRELATION_ID_HEADER, current_correlation_id, get_logger
from copilot.schemas.core import SourceRef

__all__ = [
    "ExtractionError",
    "VLMExtractor",
    "LabExtraction",
    "LabObservationExtraction",
    "IntakeExtraction",
    "DemographicsExtraction",
    "CitedListExtraction",
    "extract_lab",
    "extract_intake",
]

logger = get_logger(__name__)

# Vision model — Opus 4.8 by default (configurable). The exact model string.
_DEFAULT_VLM_MODEL = "claude-opus-4-8"

# Output-token cap for the structured extraction. A lab panel is a few dozen
# small rows; 8000 gives headroom without risking an HTTP-timeout on a
# non-streaming call.
_MAX_TOKENS = 8000

# Render density for PDF page rasterization handed to the VLM.
_RENDER_DPI = 120

# Placeholder marker in the default dev config — a key containing it is not real.
_PLACEHOLDER_MARKER = "xxxx"

# Suffix -> media type for image-only documents (no text layer / word boxes).
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_PDF_SUFFIXES = {".pdf"}


# ---------------------------------------------------------------------------
# Raw VLM extraction schemas (the first validation gate)
# ---------------------------------------------------------------------------
#
# These bind the ``messages.parse`` call. They carry only what the VLM can
# truthfully read from the page — never the FHIR patient link or a bounding box
# (both are attached by this module, not the model). ``extra="forbid"`` so an
# unexpected key is a contract violation; nullable-but-required fields force the
# model to emit an explicit ``null`` rather than silently omit or guess.


class LabObservationExtraction(BaseModel):
    """One result line as read by the VLM, plus the verbatim quote it read."""

    model_config = ConfigDict(extra="forbid")

    test_name: str = Field(min_length=1)
    value: str | None
    unit: str | None
    reference_range: str | None
    collection_date: date | None
    abnormal_flag: AbnormalFlag
    quote: str = Field(
        min_length=1,
        description="Verbatim text the value/line was read from (used to locate its box).",
    )


class LabExtraction(BaseModel):
    """Raw, schema-validated lab extraction — pre-grounding, pre-patient-link."""

    model_config = ConfigDict(extra="forbid")

    patient_mrn: str | None
    report_date: date | None
    observations: list[LabObservationExtraction]
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    source_quote: str = Field(
        min_length=1,
        description="Verbatim header/text grounding the report as a whole.",
    )


class DemographicsExtraction(BaseModel):
    """Patient-reported demographics as read by the VLM, plus its quote."""

    model_config = ConfigDict(extra="forbid")

    name: str | None
    dob: date | None
    sex: str | None
    quote: str = Field(min_length=1)


class CitedListExtraction(BaseModel):
    """A read list (meds / allergies / family hx) plus the quote grounding it."""

    model_config = ConfigDict(extra="forbid")

    items: list[str] = Field(default_factory=list)
    quote: str = Field(min_length=1)


class IntakeExtraction(BaseModel):
    """Raw, schema-validated intake extraction — pre-grounding."""

    model_config = ConfigDict(extra="forbid")

    demographics: DemographicsExtraction
    chief_concern: str | None
    chief_concern_quote: str | None
    current_medications: CitedListExtraction
    allergies: CitedListExtraction
    family_history: CitedListExtraction
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    source_quote: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExtractionError(RuntimeError):
    """VLM extraction failed and produced no trustworthy structured document.

    ``retriable`` distinguishes a transient failure (network blip, 5xx, 429)
    from a permanent one (refusal, parse/schema failure, missing key,
    unsupported input). Messages never contain document values (no PHI).
    """

    def __init__(self, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable


# ---------------------------------------------------------------------------
# Page rendering + word boxes (input to the VLM and to grounding)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Page:
    """One page: image bytes for the VLM, and (for PDFs) its word boxes."""

    number: int  # 1-indexed
    media_type: str
    image_b64: str
    words: list[dict[str, Any]] = field(default_factory=list)


def _render_pdf(path: Path) -> list[_Page]:
    """Render each PDF page to a PNG for the VLM and collect pdfplumber words."""

    pages: list[_Page] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = page.extract_words() or []
            rendered = page.to_image(resolution=_RENDER_DPI)
            buf = io.BytesIO()
            rendered.original.save(buf, format="PNG")
            b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
            pages.append(_Page(number=i, media_type="image/png", image_b64=b64, words=words))
    return pages


def _load_image(path: Path) -> list[_Page]:
    """Load an image-only document as a single page (no extractable word boxes)."""

    media_type = _IMAGE_MEDIA_TYPES[path.suffix.lower()]
    b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return [_Page(number=1, media_type=media_type, image_b64=b64, words=[])]


def _render(path: Path) -> list[_Page]:
    suffix = path.suffix.lower()
    if suffix in _PDF_SUFFIXES:
        return _render_pdf(path)
    if suffix in _IMAGE_MEDIA_TYPES:
        return _load_image(path)
    raise ExtractionError(f"unsupported document type: {suffix!r}", retriable=False)


# ---------------------------------------------------------------------------
# Grounding: map a verbatim quote to a word-box citation
# ---------------------------------------------------------------------------


def _find_box(words: list[dict[str, Any]], quote: str) -> tuple[float, float, float, float] | None:
    """Locate ``quote`` as a contiguous run of words; return its unioned box.

    Returns ``(x0, top, x1, bottom)`` in PDF points, or ``None`` when the quote
    could not be located (no text layer, or the model quoted text not present
    verbatim). Whitespace is normalized to tokens so a multi-word quote unions
    the boxes of the words that make it up.
    """

    tokens = quote.split()
    if not tokens or not words:
        return None
    texts = [str(w["text"]) for w in words]
    span = len(tokens)
    for i in range(len(texts) - span + 1):
        if texts[i : i + span] == tokens:
            run = words[i : i + span]
            return (
                min(float(w["x0"]) for w in run),
                min(float(w["top"]) for w in run),
                max(float(w["x1"]) for w in run),
                max(float(w["bottom"]) for w in run),
            )
    return None


def _cite(
    *,
    source_type: str,
    source_id: str,
    quote: str,
    pages: list[_Page],
) -> tuple[SourceCitation, bool]:
    """Build a citation for ``quote``, box-grounded when possible.

    Returns ``(citation, grounded)`` where ``grounded`` is True only when a word
    box was found. When not found, the citation degrades to page-level (page 1),
    which is the graceful-degradation path for image-only scans.
    """

    value = quote if quote else source_id
    for page in pages:
        box = _find_box(page.words, quote)
        if box is not None:
            x0, top, x1, bottom = box
            return (
                SourceCitation(
                    source_type=source_type,  # type: ignore[arg-type]
                    source_id=source_id,
                    page_or_section=f"page {page.number}",
                    field_or_chunk_id=f"bbox={x0:.1f},{top:.1f},{x1:.1f},{bottom:.1f}",
                    quote_or_value=value,
                ),
                True,
            )
    first_page = pages[0].number if pages else 1
    return (
        SourceCitation(
            source_type=source_type,  # type: ignore[arg-type]
            source_id=source_id,
            page_or_section=f"page {first_page}",
            field_or_chunk_id=None,
            quote_or_value=value,
        ),
        False,
    )


def _clamp_confidence(vlm_confidence: float, grounded: int, total: int) -> float:
    """Clamp the VLM's self-report by the fraction of values that were grounded."""

    ratio = 1.0 if total == 0 else grounded / total
    return min(vlm_confidence, ratio)


# ---------------------------------------------------------------------------
# Assembly: raw extraction -> validated PRP-02 contract (second gate)
# ---------------------------------------------------------------------------


def _assemble_lab(raw: LabExtraction, source_id: str, pages: list[_Page]) -> LabReport:
    observations: list[LabObservation] = []
    grounded = 0
    for row in raw.observations:
        quote = row.quote or row.value or row.test_name
        citation, ok = _cite(
            source_type="lab_pdf", source_id=source_id, quote=quote, pages=pages
        )
        grounded += int(ok)
        observations.append(
            LabObservation(
                test_name=row.test_name,
                value=row.value,
                unit=row.unit,
                reference_range=row.reference_range,
                collection_date=row.collection_date,
                abnormal_flag=row.abnormal_flag,
                citation=citation,
            )
        )

    source, _ = _cite(
        source_type="lab_pdf", source_id=source_id, quote=raw.source_quote, pages=pages
    )
    # The FHIR patient link is resolved downstream (PRP-06); here we ground the
    # report to its reported MRN so the pointer is never invented from nothing.
    patient_ref = SourceRef(resource_type="Patient", id=raw.patient_mrn or "unknown")
    return LabReport(
        patient_ref=patient_ref,
        report_date=raw.report_date,
        observations=observations,
        extraction_confidence=_clamp_confidence(
            raw.extraction_confidence, grounded, len(observations)
        ),
        source=source,
    )


def _assemble_intake(raw: IntakeExtraction, source_id: str, pages: list[_Page]) -> IntakeFacts:
    grounded = 0
    total = 0

    def cite(quote: str) -> SourceCitation:
        nonlocal grounded, total
        total += 1
        citation, ok = _cite(
            source_type="intake_form", source_id=source_id, quote=quote, pages=pages
        )
        grounded += int(ok)
        return citation

    demographics = IntakeDemographics(
        name=raw.demographics.name,
        dob=raw.demographics.dob,
        sex=raw.demographics.sex,
        citation=cite(raw.demographics.quote),
    )

    chief_concern: CitedText | None = None
    if raw.chief_concern is not None:
        chief_concern = CitedText(
            text=raw.chief_concern,
            citation=cite(raw.chief_concern_quote or raw.chief_concern),
        )

    medications = CitedList(
        items=raw.current_medications.items, citation=cite(raw.current_medications.quote)
    )
    allergies = CitedList(items=raw.allergies.items, citation=cite(raw.allergies.quote))
    family_history = CitedList(
        items=raw.family_history.items, citation=cite(raw.family_history.quote)
    )

    source, _ = _cite(
        source_type="intake_form", source_id=source_id, quote=raw.source_quote, pages=pages
    )
    return IntakeFacts(
        demographics=demographics,
        chief_concern=chief_concern,
        current_medications=medications,
        allergies=allergies,
        family_history=family_history,
        extraction_confidence=_clamp_confidence(raw.extraction_confidence, grounded, total),
        source=source,
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_LAB_SYSTEM = """\
You are a clinical document extraction tool. You are given page image(s) of a \
laboratory report. Read it faithfully and return the structured result.

Hard rules:
- Transcribe only what is legibly present. If a field (value, unit, reference \
range, collection date, MRN, report date) cannot be read with confidence, emit \
`null` for it — NEVER guess, infer, or fill in a plausible value.
- For every observation, put in `quote` the shortest verbatim text you read the \
result value from (e.g. the number as printed). This is used to locate the value \
on the page; copy it exactly, character for character.
- `abnormal_flag` is your reading of the result interpretation; use `unknown` \
when it cannot be determined.
- `extraction_confidence` in [0,1] is your honest self-assessment of how \
reliably you could read this document (low for blurry/partial scans)."""

_LAB_INSTRUCTION = (
    "Extract every laboratory observation from this report into the structured "
    "schema. Remember: unreadable fields are null, and each observation's `quote` "
    "must be the exact text you read the value from."
)

_INTAKE_SYSTEM = """\
You are a clinical document extraction tool. You are given page image(s) of a \
patient intake form. Read it faithfully and return the structured result.

Hard rules:
- Transcribe only what is legibly present. If a field cannot be read with \
confidence, emit `null` (or an empty list where a list is expected) — NEVER \
guess or invent a value. An empty medication/allergy/family-history list means \
"none reported", which is different from "not read".
- For each grounded fact, put in `quote` the shortest verbatim text it was read \
from; copy it exactly so it can be located on the page.
- `extraction_confidence` in [0,1] is your honest self-assessment of legibility."""

_INTAKE_INSTRUCTION = (
    "Extract the demographics, chief concern, current medications, allergies, and "
    "family history from this intake form into the structured schema. Unreadable "
    "fields are null; each `quote` must be exact verbatim text from the page."
)


# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    """True for Anthropic failures worth retrying (network, timeout, 429, 5xx)."""

    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500
    return False


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class VLMExtractor:
    """Extract a validated ``LabReport`` / ``IntakeFacts`` from a document.

    ``client`` may be injected (tests wire an :class:`AsyncAnthropic` to a mock
    transport); otherwise one is constructed lazily from :class:`Settings` on
    first use, which is also when a missing API key is surfaced.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: AsyncAnthropic | None = None,
        model: str | None = None,
        max_attempts: int = 3,
        timeout: float = 60.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._model = model or os.getenv("COPILOT_VLM_MODEL") or _DEFAULT_VLM_MODEL
        self._max_attempts = max_attempts
        self._timeout = timeout

    # -- lifecycle ---------------------------------------------------------

    def _anthropic(self) -> AsyncAnthropic:
        """Return the Anthropic client, constructing it (and validating the key)."""

        if self._client is None:
            key = self._settings.anthropic_api_key
            if not key or _PLACEHOLDER_MARKER in key.lower():
                raise ExtractionError(
                    "ANTHROPIC_API_KEY is not configured; set a real key in the "
                    "environment / .env before calling the VLM.",
                    retriable=False,
                )
            self._client = AsyncAnthropic(api_key=key)
        return self._client

    # -- public API --------------------------------------------------------

    async def extract_lab(self, file_path: str | Path) -> LabReport:
        """Extract a grounded, schema-validated :class:`LabReport` from a document."""

        path = Path(file_path)
        pages = _render(path)
        raw = await self._extract(
            pages,
            system=_LAB_SYSTEM,
            instruction=_LAB_INSTRUCTION,
            output_format=LabExtraction,
        )
        report = _assemble_lab(raw, path.name, pages)
        logger.info(
            "vlm.extract_lab.ok",
            model=self._model,
            source=path.name,
            pages=len(pages),
            observations=len(report.observations),
        )
        return report

    async def extract_intake(self, file_path: str | Path) -> IntakeFacts:
        """Extract grounded, schema-validated :class:`IntakeFacts` from a document."""

        path = Path(file_path)
        pages = _render(path)
        raw = await self._extract(
            pages,
            system=_INTAKE_SYSTEM,
            instruction=_INTAKE_INSTRUCTION,
            output_format=IntakeExtraction,
        )
        facts = _assemble_intake(raw, path.name, pages)
        logger.info(
            "vlm.extract_intake.ok",
            model=self._model,
            source=path.name,
            pages=len(pages),
        )
        return facts

    # -- internals ---------------------------------------------------------

    def _user_content(self, pages: list[_Page], instruction: str) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": page.media_type,
                    "data": page.image_b64,
                },
            }
            for page in pages
        ]
        content.append({"type": "text", "text": instruction})
        return content

    async def _extract(
        self,
        pages: list[_Page],
        *,
        system: str,
        instruction: str,
        output_format: type[BaseModel],
    ) -> Any:
        """Call the VLM with the structured-output schema attached; validate."""

        client = self._anthropic()
        content = self._user_content(pages, instruction)
        cid = current_correlation_id()
        extra_headers = {CORRELATION_ID_HEADER: cid} if cid else None

        message: Any = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=0.2, max=2.0),
                retry=retry_if_exception(_is_transient),
                reraise=True,
            ):
                with attempt:
                    message = await client.messages.parse(
                        model=self._model,
                        max_tokens=_MAX_TOKENS,
                        system=system,
                        messages=[{"role": "user", "content": content}],
                        output_format=output_format,
                        extra_headers=extra_headers,
                        timeout=self._timeout,
                    )
        except pydantic.ValidationError as exc:
            # Raw VLM output did not satisfy the extraction schema — reject, do
            # not surface a partially-parsed or guessed document.
            raise ExtractionError(
                "the VLM's structured output did not satisfy the extraction schema.",
                retriable=False,
            ) from exc
        except (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError) as exc:
            raise ExtractionError(
                "the VLM call failed to complete after retries.", retriable=True
            ) from exc
        except APIStatusError as exc:
            raise ExtractionError(
                f"the VLM call failed with HTTP {exc.status_code}.",
                retriable=exc.status_code >= 500,
            ) from exc

        if message.stop_reason == "refusal":
            logger.warning("vlm.extract.refusal", model=self._model)
            raise ExtractionError(
                "the VLM refused to extract this document; surfacing rather than "
                "fabricating output.",
                retriable=False,
            )

        parsed = message.parsed_output
        if parsed is None:
            logger.warning("vlm.extract.unparseable", model=self._model)
            raise ExtractionError(
                "the VLM returned no parseable structured extraction.",
                retriable=False,
            )
        return parsed


# ---------------------------------------------------------------------------
# Module-level convenience API (the PRP-04 contract)
# ---------------------------------------------------------------------------


async def extract_lab(file_path: str | Path) -> LabReport:
    """Extract a validated :class:`LabReport` using a default extractor."""

    return await VLMExtractor().extract_lab(file_path)


async def extract_intake(file_path: str | Path) -> IntakeFacts:
    """Extract validated :class:`IntakeFacts` using a default extractor."""

    return await VLMExtractor().extract_intake(file_path)
