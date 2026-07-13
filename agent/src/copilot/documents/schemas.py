"""Canonical extraction contracts for clinical documents (FR-6).

These models are the **source of truth** that raw VLM output must pass
through before anything downstream trusts it. A lab PDF or intake form is read
by the vision model, and the messy result is parsed into one of these strict
schemas; malformed or hallucinated output is rejected here at the schema layer
rather than reaching OpenEMR.

`SourceCitation` is the machine-readable grounding contract for the whole
Week-2 answer path: every extracted fact points back at the exact document,
page/section, and quoted evidence it came from. It complements — and does not
replace — the FHIR-oriented `SourceRef` from `schemas/core.py`, which is reused
here (e.g. `LabReport.patient_ref`) rather than duplicated.

Design notes mirror `schemas/core.py`:
- Pydantic v2 throughout.
- Every model is `frozen` so it behaves as an immutable identity once built.
- `extra="forbid"` everywhere: an unexpected key is a contract violation.
- Fields that can be "not found" are typed `... | None` and required to be
  present, so the extractor must *explicitly* emit `null` — a value is never
  silently invented or silently omitted.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from copilot.schemas.core import SourceRef

__all__ = [
    "SourceType",
    "AbnormalFlag",
    "SourceCitation",
    "CitedText",
    "CitedList",
    "IntakeDemographics",
    "LabObservation",
    "LabReport",
    "IntakeFacts",
]

# Where an extracted fact was grounded. `fhir` covers facts read back from an
# existing OpenEMR record rather than a freshly-ingested document.
SourceType = Literal["lab_pdf", "intake_form", "guideline", "fhir"]

# Result interpretation for a lab observation. `unknown` is the explicit flag
# for "could not be determined" so absence is never mistaken for `normal`.
AbnormalFlag = Literal["normal", "high", "low", "critical", "unknown"]


class SourceCitation(BaseModel):
    """A machine-readable pointer to the exact evidence for a fact (FR-6).

    Every extracted clinical fact carries one of these so grounding is enforced
    by output *shape*, not by prompting: it names the document, the page or
    section within it, the specific field or retrieval chunk, and the verbatim
    quote (or value) the fact was read from. `page_or_section` and
    `field_or_chunk_id` are locator hints that depend on the source kind (a PDF
    page vs. a form field vs. a RAG chunk id); at least one is typically set.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SourceType = Field(description="Kind of source the fact was grounded in.")
    source_id: str = Field(
        min_length=1,
        description="Stable id of the source document / record, e.g. a filename or FHIR id.",
    )
    page_or_section: str | None = Field(
        default=None,
        description="PDF page or guideline section the fact came from, if applicable.",
    )
    field_or_chunk_id: str | None = Field(
        default=None,
        description="Intake-form field name or RAG chunk id the fact came from, if applicable.",
    )
    quote_or_value: str = Field(
        min_length=1,
        description="Verbatim quote or value read from the source (the grounding evidence).",
    )


class CitedText(BaseModel):
    """A single free-text fact paired with its grounding citation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, description="The extracted text.")
    citation: SourceCitation = Field(description="Where the text was grounded.")


class CitedList(BaseModel):
    """A list of text facts sharing one grounding citation.

    An empty `items` list is meaningful — "the patient reported none" — while
    the citation still records where in the document that determination was
    made. Absence is therefore stated, never inferred.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[str] = Field(
        default_factory=list,
        description="The extracted entries; empty means explicitly 'none reported'.",
    )
    citation: SourceCitation = Field(description="Where the list was grounded.")


class IntakeDemographics(BaseModel):
    """Patient-reported demographics from an intake form, grounded by a citation.

    Each field is nullable and required to be present: an unread field is an
    explicit `null`, never a guessed value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(description="Reported name, or null if not found.")
    dob: date | None = Field(description="Reported date of birth, or null if not found.")
    sex: str | None = Field(description="Reported sex, or null if not found.")
    citation: SourceCitation = Field(description="Where the demographics were grounded.")


class LabObservation(BaseModel):
    """A single result line extracted from a lab report (FR-6).

    `test_name`, `abnormal_flag`, and `citation` are always known; the measured
    `value`, `unit`, `reference_range`, and `collection_date` are nullable and
    required to be present, so a field the model could not read is an explicit
    `null` rather than an invented number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    test_name: str = Field(min_length=1, description="Analyte / test display name.")
    value: str | None = Field(description="Result value as text, or null if not found.")
    unit: str | None = Field(description="Unit of measure, or null if not found.")
    reference_range: str | None = Field(
        description="Reference range as text, or null if not found.",
    )
    collection_date: date | None = Field(
        description="Specimen collection date, or null if not found.",
    )
    abnormal_flag: AbnormalFlag = Field(
        description="Result interpretation; 'unknown' when it could not be determined.",
    )
    citation: SourceCitation = Field(description="Pointer to the source line (grounding).")


class LabReport(BaseModel):
    """A structured lab report extracted from a PDF (FR-6).

    Composes with the FHIR-oriented `SourceRef` (`patient_ref`) so an extracted
    report links back to the OpenEMR patient it belongs to, while `source`
    grounds the report itself in the originating document.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    patient_ref: SourceRef = Field(description="Pointer to the FHIR patient the report is for.")
    report_date: date | None = Field(description="Report date, or null if not found.")
    observations: list[LabObservation] = Field(
        default_factory=list,
        description="The result lines extracted from the report.",
    )
    extraction_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model's confidence in the extraction, in [0, 1].",
    )
    source: SourceCitation = Field(description="Pointer to the source document (grounding).")


class IntakeFacts(BaseModel):
    """Patient-reported facts extracted from an intake form (FR-6).

    Every clinical field is individually grounded: `chief_concern` carries its
    own citation, and the medication / allergy / family-history lists each carry
    theirs, so no fact is surfaced without a pointer back to the form. A
    `chief_concern` of `null` means the field was read and found empty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    demographics: IntakeDemographics = Field(description="Grounded patient demographics.")
    chief_concern: CitedText | None = Field(
        description="Grounded chief concern, or null if the form left it blank.",
    )
    current_medications: CitedList = Field(description="Grounded current-medication list.")
    allergies: CitedList = Field(description="Grounded allergy list.")
    family_history: CitedList = Field(description="Grounded family-history list.")
    extraction_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model's confidence in the extraction, in [0, 1].",
    )
    source: SourceCitation = Field(description="Pointer to the source document (grounding).")
