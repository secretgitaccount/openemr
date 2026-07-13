"""The FR-1 ingestion tool: ``attach_and_extract`` (PRP-06).

One traced, correlation-ID-carrying operation that ties PRP-04 (VLM extraction)
and PRP-05 (idempotent OpenEMR writes) together:

    store the source document → extract structured facts → persist the derived
    facts → return the strict :class:`IngestResult` (source ref, the validated
    extracted model, the persisted OpenEMR record refs, and a confidence).

The order is deliberate. The source PDF is stored **first** so a citation always
has a durable record to point back at even if a later step degrades; extraction
then runs schema-bound (nothing the VLM emits reaches a caller un-validated);
finally the derived values are persisted idempotently — re-ingesting the same
file upserts rather than duplicates (FR-10), because both writes are content-
addressed inside PRP-05.

Only lab reports have a derived-record write path in PRP-05
(:func:`persist_observations`), so an ``intake_form`` ingest stores its source
and returns the validated :class:`IntakeFacts` with no derived ``record_refs`` —
never a silently invented persistence. A single ``correlation_id`` is bound for
the whole operation (a new one is minted only when the caller has not already
set one), so the store / extract / persist steps share one trace and no PHI is
logged (only ids, counts, and the doc type).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from copilot.documents.extract import VLMExtractor, extract_intake, extract_lab
from copilot.documents.openemr_write import (
    OpenEmrWriter,
    persist_observations,
    store_source,
)
from copilot.documents.schemas import IntakeFacts, LabReport
from copilot.logging import (
    current_correlation_id,
    get_logger,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from copilot.schemas.core import SourceRef

__all__ = [
    "DOC_TYPES",
    "IngestError",
    "IngestResult",
    "attach_and_extract",
]

logger = get_logger(__name__)

#: The document kinds this tool accepts. ``lab_pdf`` extracts a
#: :class:`LabReport` (with a derived-vitals persistence path); ``intake_form``
#: extracts :class:`IntakeFacts` (source-stored only — PRP-05 has no intake
#: persistence path yet).
DOC_TYPES: frozenset[str] = frozenset({"lab_pdf", "intake_form"})


class IngestError(ValueError):
    """The ingestion request itself was malformed (e.g. an unknown ``doc_type``).

    Raised before any network/VLM work so the HTTP layer can turn it into a 422.
    Distinct from the typed extraction / write errors raised by the composed
    steps, which carry their own retriability.
    """


class IngestResult(BaseModel):
    """The FR-1 tool result: source ref + validated facts + persisted refs.

    ``source`` points at the stored source document, ``extracted`` is the
    schema-validated facts (each value already carrying its own
    :class:`~copilot.documents.schemas.SourceCitation`), ``record_refs`` are the
    OpenEMR records the derived facts were persisted to (the ingestion encounter
    and any vitals), and ``confidence`` mirrors the extraction's self-assessed,
    grounding-clamped confidence. Frozen + ``extra="forbid"`` like every other
    contract in the codebase.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SourceRef = Field(description="Pointer to the stored source document.")
    extracted: LabReport | IntakeFacts = Field(
        description="The schema-validated extracted facts (per-value citations included).",
    )
    record_refs: list[SourceRef] = Field(
        default_factory=list,
        description="OpenEMR records the derived facts were persisted to (encounter + vitals).",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Extraction confidence (VLM self-report, clamped by grounding).",
    )


async def _extract(
    file_path: Path, doc_type: str, extractor: VLMExtractor | None
) -> LabReport | IntakeFacts:
    """Run the doc-type-appropriate extractor (an injected one wins, for tests)."""

    if doc_type == "lab_pdf":
        if extractor is not None:
            return await extractor.extract_lab(file_path)
        return await extract_lab(file_path)
    # intake_form (the only remaining member of DOC_TYPES)
    if extractor is not None:
        return await extractor.extract_intake(file_path)
    return await extract_intake(file_path)


async def _persist(
    patient_id: str,
    extracted: LabReport | IntakeFacts,
    writer: OpenEmrWriter | None,
) -> list[SourceRef]:
    """Persist derived facts. Only lab reports have a PRP-05 write path."""

    if isinstance(extracted, LabReport):
        return await persist_observations(patient_id, extracted, writer=writer)
    # Intake facts have no derived-record write path in PRP-05; the source
    # document itself is the durable record. Nothing is invented.
    return []


async def attach_and_extract(
    patient_id: str,
    file_path: str | Path,
    doc_type: str,
    *,
    extractor: VLMExtractor | None = None,
    writer: OpenEmrWriter | None = None,
) -> IngestResult:
    """Store, extract, and persist a clinical document as one traced operation.

    Composes PRP-04 extraction and PRP-05 persistence in the order *store source
    → extract → persist → return*. ``doc_type`` must be one of :data:`DOC_TYPES`
    (else :class:`IngestError`). ``extractor`` / ``writer`` may be injected for
    testing (stubbed VLM + mocked OpenEMR); in production both default to the
    key-/OAuth-wired module-level tools. A correlation id is bound for the whole
    operation so all three steps share one trace.
    """

    doc_type = (doc_type or "").strip()
    if doc_type not in DOC_TYPES:
        raise IngestError(
            f"unknown doc_type {doc_type!r}; expected one of {sorted(DOC_TYPES)}"
        )

    path = Path(file_path)

    # Bind one correlation id for the whole ingest so store/extract/persist share
    # a trace. Only mint one when the caller (e.g. the HTTP middleware) has not
    # already set one; restore the prior context on exit.
    token = None if current_correlation_id() else set_correlation_id(new_correlation_id())
    try:
        logger.info("ingest.start", doc_type=doc_type, source=path.name)

        source = await store_source(patient_id, str(path), doc_type, writer=writer)
        extracted = await _extract(path, doc_type, extractor)
        record_refs = await _persist(patient_id, extracted, writer)

        result = IngestResult(
            source=source,
            extracted=extracted,
            record_refs=record_refs,
            confidence=extracted.extraction_confidence,
        )
        logger.info(
            "ingest.ok",
            doc_type=doc_type,
            source_id=source.id,
            record_refs=len(record_refs),
        )
        return result
    finally:
        if token is not None:
            reset_correlation_id(token)
