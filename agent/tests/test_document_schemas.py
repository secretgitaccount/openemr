"""Contract tests for the Week-2 document extraction schemas (PRP-02).

Each case exercises the strict-parsing invariants that make these models the
source of truth for VLM output: unknown keys are rejected, grounding citations
are mandatory, `abnormal_flag` is a closed enum, and every model survives a
JSON round-trip unchanged. Per FR-6 malformed extraction is rejected here at
the schema layer rather than reaching OpenEMR.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest
from pydantic import ValidationError

from copilot.documents.schemas import (
    CitedList,
    CitedText,
    IntakeDemographics,
    IntakeFacts,
    LabObservation,
    LabReport,
    SourceCitation,
)
from copilot.schemas.core import SourceRef


# --- sample payloads -------------------------------------------------------


def _valid_citation() -> dict[str, object]:
    return {
        "source_type": "lab_pdf",
        "source_id": "report-42.pdf",
        "page_or_section": "1",
        "field_or_chunk_id": None,
        "quote_or_value": "HbA1c 7.1 %",
    }


def _valid_observation() -> dict[str, object]:
    return {
        "test_name": "HbA1c",
        "value": "7.1",
        "unit": "%",
        "reference_range": "4.0-5.6",
        "collection_date": "2026-06-01",
        "abnormal_flag": "high",
        "citation": _valid_citation(),
    }


def _valid_lab_report() -> dict[str, object]:
    return {
        "patient_ref": {"resource_type": "Patient", "id": "p-1"},
        "report_date": "2026-06-02",
        "observations": [_valid_observation()],
        "extraction_confidence": 0.92,
        "source": {**_valid_citation(), "source_type": "lab_pdf"},
    }


def _valid_intake() -> dict[str, object]:
    intake_cite = {**_valid_citation(), "source_type": "intake_form"}
    return {
        "demographics": {
            "name": "Jane Doe",
            "dob": "1990-04-01",
            "sex": "female",
            "citation": intake_cite,
        },
        "chief_concern": {"text": "persistent cough", "citation": intake_cite},
        "current_medications": {"items": ["Metformin"], "citation": intake_cite},
        "allergies": {"items": [], "citation": intake_cite},
        "family_history": {"items": ["diabetes"], "citation": intake_cite},
        "extraction_confidence": 0.8,
        "source": intake_cite,
    }


# --- SourceCitation --------------------------------------------------------


def test_source_citation_parses_valid() -> None:
    cite = SourceCitation.model_validate(_valid_citation())
    assert cite.source_type == "lab_pdf"
    assert cite.field_or_chunk_id is None
    assert cite.quote_or_value == "HbA1c 7.1 %"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(source_type="ehr_note"),  # not in the enum
        lambda c: c.update(source_id=""),  # empty id
        lambda c: c.update(quote_or_value=""),  # empty grounding evidence
        lambda c: c.pop("quote_or_value"),  # missing grounding evidence
        lambda c: c.update(extra=1),  # unexpected key
    ],
)
def test_source_citation_rejects_invalid(mutate: Callable[[dict], object]) -> None:
    payload = _valid_citation()
    mutate(payload)
    with pytest.raises(ValidationError):
        SourceCitation.model_validate(payload)


# --- LabObservation --------------------------------------------------------


def test_lab_observation_parses_valid() -> None:
    obs = LabObservation.model_validate(_valid_observation())
    assert obs.collection_date == date(2026, 6, 1)
    assert obs.abnormal_flag == "high"
    assert isinstance(obs.citation, SourceCitation)


def test_lab_observation_allows_explicit_null_measurements() -> None:
    """A not-found value is an explicit null, never invented."""
    payload = {**_valid_observation(), "value": None, "unit": None, "reference_range": None}
    obs = LabObservation.model_validate(payload)
    assert obs.value is None
    assert obs.abnormal_flag == "high"


@pytest.mark.parametrize(
    "flag",
    ["", "abnormal", "HIGH", "elevated", None],
)
def test_lab_observation_rejects_out_of_enum_flag(flag: object) -> None:
    payload = {**_valid_observation(), "abnormal_flag": flag}
    with pytest.raises(ValidationError):
        LabObservation.model_validate(payload)


def test_lab_observation_requires_citation() -> None:
    payload = _valid_observation()
    payload.pop("citation")
    with pytest.raises(ValidationError):
        LabObservation.model_validate(payload)


def test_lab_observation_rejects_unknown_key() -> None:
    payload = {**_valid_observation(), "unexpected": 1}
    with pytest.raises(ValidationError):
        LabObservation.model_validate(payload)


# --- LabReport -------------------------------------------------------------


def test_lab_report_parses_valid() -> None:
    report = LabReport.model_validate(_valid_lab_report())
    assert isinstance(report.patient_ref, SourceRef)
    assert report.observations[0].test_name == "HbA1c"
    assert report.extraction_confidence == pytest.approx(0.92)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("source"),  # missing grounding citation
        lambda p: p.pop("patient_ref"),  # missing patient pointer
        lambda p: p.update(extraction_confidence=1.5),  # out of [0, 1]
        lambda p: p.update(extraction_confidence=-0.1),  # out of [0, 1]
        lambda p: p.update(unexpected=1),  # unknown key
    ],
)
def test_lab_report_rejects_invalid(mutate: Callable[[dict], object]) -> None:
    payload = _valid_lab_report()
    mutate(payload)
    with pytest.raises(ValidationError):
        LabReport.model_validate(payload)


def test_lab_report_rejects_malformed_observation() -> None:
    payload = _valid_lab_report()
    payload["observations"] = [{"test_name": "HbA1c"}]  # missing required fields
    with pytest.raises(ValidationError):
        LabReport.model_validate(payload)


# --- IntakeFacts -----------------------------------------------------------


def test_intake_facts_parses_valid() -> None:
    intake = IntakeFacts.model_validate(_valid_intake())
    assert intake.demographics.name == "Jane Doe"
    assert intake.chief_concern is not None
    assert intake.chief_concern.text == "persistent cough"
    assert intake.allergies.items == []
    assert intake.current_medications.citation.source_type == "intake_form"


def test_intake_facts_allows_blank_chief_concern() -> None:
    payload = {**_valid_intake(), "chief_concern": None}
    intake = IntakeFacts.model_validate(payload)
    assert intake.chief_concern is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("source"),  # missing report-level grounding
        lambda p: p["demographics"].pop("citation"),  # ungrounded demographics
        lambda p: p["current_medications"].pop("citation"),  # ungrounded list
        lambda p: p["chief_concern"].pop("citation"),  # ungrounded chief concern
        lambda p: p.update(extraction_confidence=2.0),  # out of [0, 1]
        lambda p: p.update(unexpected=1),  # unknown key
    ],
)
def test_intake_facts_rejects_invalid(mutate: Callable[[dict], object]) -> None:
    payload = _valid_intake()
    mutate(payload)
    with pytest.raises(ValidationError):
        IntakeFacts.model_validate(payload)


# --- frozen / immutability -------------------------------------------------


def test_models_are_frozen() -> None:
    cite = SourceCitation.model_validate(_valid_citation())
    with pytest.raises(ValidationError):
        cite.source_id = "other"  # type: ignore[misc]


# --- JSON round-trip -------------------------------------------------------


def test_source_citation_round_trips() -> None:
    original = SourceCitation.model_validate(_valid_citation())
    assert SourceCitation.model_validate_json(original.model_dump_json()) == original


def test_lab_observation_round_trips() -> None:
    original = LabObservation.model_validate(_valid_observation())
    restored = LabObservation.model_validate_json(original.model_dump_json())
    assert restored == original


def test_lab_report_round_trips() -> None:
    original = LabReport.model_validate(_valid_lab_report())
    restored = LabReport.model_validate_json(original.model_dump_json())
    assert restored == original


def test_intake_facts_round_trips() -> None:
    original = IntakeFacts.model_validate(_valid_intake())
    restored = IntakeFacts.model_validate_json(original.model_dump_json())
    assert restored == original


def test_cited_helpers_round_trip() -> None:
    text = CitedText.model_validate(
        {"text": "note", "citation": {**_valid_citation(), "source_type": "guideline"}}
    )
    lst = CitedList.model_validate({"items": ["a", "b"], "citation": _valid_citation()})
    demo = IntakeDemographics.model_validate(
        {"name": None, "dob": None, "sex": None, "citation": _valid_citation()}
    )
    assert CitedText.model_validate_json(text.model_dump_json()) == text
    assert CitedList.model_validate_json(lst.model_dump_json()) == lst
    assert IntakeDemographics.model_validate_json(demo.model_dump_json()) == demo
