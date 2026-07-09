"""Contract tests for the M1 clinical + source-bound output schemas (PRP M1-1).

Each case parses a valid sample and rejects an invalid one, confirms every
record type carries a `SourceRef` grounding pointer, exercises the `Claim`
grounding invariant, and round-trips the `CriticalSet` bundle. Per NFR-3 the
schemas are the source of truth and malformed data is rejected at this layer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from copilot.schemas.clinical import (
    Allergy,
    CriticalSet,
    Deltas,
    Encounter,
    LabResult,
    Medication,
    PanelDecision,
    Problem,
    ScheduledPatient,
)
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

_SOURCE = {"resource_type": "Observation", "id": "obs-1"}


# --- ScheduledPatient ------------------------------------------------------


def test_scheduled_patient_parses_valid() -> None:
    sp = ScheduledPatient.model_validate(
        {
            "patient_id": "p-1",
            "name": "Jane Doe",
            "start": "2026-07-07T09:00:00Z",
            "appointment_id": "appt-1",
            "source": {"resource_type": "Appointment", "id": "appt-1"},
        }
    )
    assert sp.patient_id == "p-1"
    assert sp.start == datetime(2026, 7, 7, 9, 0, tzinfo=UTC)
    assert isinstance(sp.source, SourceRef)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("source"),  # missing grounding
        lambda p: p.update(name=""),  # empty name
        lambda p: p.update(start="not-a-datetime"),  # unparseable
        lambda p: p.update(extra=1),  # unexpected key
    ],
)
def test_scheduled_patient_rejects_invalid(mutate: Callable[[dict], object]) -> None:
    payload = {
        "patient_id": "p-1",
        "name": "Jane Doe",
        "start": "2026-07-07T09:00:00Z",
        "appointment_id": "appt-1",
        "source": {"resource_type": "Appointment", "id": "appt-1"},
    }
    mutate(payload)
    with pytest.raises(ValidationError):
        ScheduledPatient.model_validate(payload)


# --- PanelDecision ---------------------------------------------------------


def test_panel_decision_defaults() -> None:
    decision = PanelDecision(in_panel=True, reason="on the clinician's panel")
    assert decision.break_glass is False
    assert decision.source is None


def test_panel_decision_break_glass_with_source() -> None:
    decision = PanelDecision(
        in_panel=False,
        reason="emergency override",
        break_glass=True,
        source=SourceRef(resource_type="Patient", id="p-9"),
    )
    assert decision.break_glass is True
    assert decision.source is not None


def test_panel_decision_rejects_empty_reason() -> None:
    with pytest.raises(ValidationError):
        PanelDecision(in_panel=True, reason="")


# --- record types that must carry a SourceRef ------------------------------


def _valid_medication() -> dict[str, object]:
    return {"id": "m-1", "name": "Metformin", "status": "active", "source": _SOURCE}


def _valid_allergy() -> dict[str, object]:
    return {"id": "a-1", "substance": "Penicillin", "source": _SOURCE}


def _valid_lab() -> dict[str, object]:
    return {"id": "l-1", "name": "HbA1c", "value": "7.1", "unit": "%", "source": _SOURCE}


def _valid_problem() -> dict[str, object]:
    return {"id": "pr-1", "name": "Type 2 diabetes", "source": _SOURCE}


def _valid_encounter() -> dict[str, object]:
    return {"id": "e-1", "source": _SOURCE}


def test_medication_parses_valid() -> None:
    med = Medication.model_validate(_valid_medication())
    assert med.dosage is None
    assert med.source.id == "obs-1"


def test_allergy_parses_valid() -> None:
    allergy = Allergy.model_validate(_valid_allergy())
    assert allergy.substance == "Penicillin"
    assert allergy.reaction is None


def test_lab_result_parses_valid() -> None:
    lab = LabResult.model_validate(
        {**_valid_lab(), "effective": "2026-06-01T12:00:00Z", "abnormal": True}
    )
    assert lab.effective == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    assert lab.abnormal is True


def test_problem_parses_valid() -> None:
    problem = Problem.model_validate({**_valid_problem(), "onset": "2020-01-15"})
    assert problem.onset == date(2020, 1, 15)


def test_encounter_parses_valid() -> None:
    enc = Encounter.model_validate({**_valid_encounter(), "kind": "ambulatory"})
    assert enc.kind == "ambulatory"
    assert enc.start is None


@pytest.mark.parametrize(
    ("model", "factory"),
    [
        (Medication, _valid_medication),
        (Allergy, _valid_allergy),
        (LabResult, _valid_lab),
        (Problem, _valid_problem),
        (Encounter, _valid_encounter),
    ],
)
def test_record_requires_source(model: type, factory: Callable[[], dict]) -> None:
    """FR-8: every clinical record must carry a SourceRef for grounding."""
    payload = factory()
    payload.pop("source")
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "factory"),
    [
        (Medication, _valid_medication),
        (Allergy, _valid_allergy),
        (LabResult, _valid_lab),
        (Problem, _valid_problem),
        (Encounter, _valid_encounter),
    ],
)
def test_record_rejects_extra_key(model: type, factory: Callable[[], dict]) -> None:
    payload = factory()
    payload["unexpected"] = 1
    with pytest.raises(ValidationError):
        model.model_validate(payload)


# --- Deltas ----------------------------------------------------------------


def test_deltas_defaults_are_empty() -> None:
    deltas = Deltas()
    assert deltas.reference_visit is None
    assert deltas.new_meds == []
    assert deltas.stopped_meds == []
    assert deltas.new_problems == []
    assert deltas.new_labs == []
    assert deltas.new_encounters == []


def test_deltas_holds_full_records() -> None:
    deltas = Deltas.model_validate(
        {
            "reference_visit": "2026-01-01T00:00:00Z",
            "new_meds": [_valid_medication()],
            "new_problems": [_valid_problem()],
        }
    )
    assert isinstance(deltas.new_meds[0], Medication)
    assert isinstance(deltas.new_problems[0], Problem)


def test_deltas_rejects_malformed_record() -> None:
    with pytest.raises(ValidationError):
        Deltas.model_validate({"new_meds": [{"id": "m-1"}]})  # missing required fields


# --- CriticalSet -----------------------------------------------------------


def test_critical_set_defaults() -> None:
    cs = CriticalSet()
    assert cs.medications == []
    assert cs.deltas is None
    assert cs.missing == []
    assert isinstance(cs.retrieved_at, datetime)


def test_critical_set_round_trips() -> None:
    cs = CriticalSet(
        medications=[Medication.model_validate(_valid_medication())],
        allergies=[Allergy.model_validate(_valid_allergy())],
        labs=[LabResult.model_validate(_valid_lab())],
        problems=[Problem.model_validate(_valid_problem())],
        deltas=Deltas(new_encounters=[Encounter.model_validate(_valid_encounter())]),
        missing=["labs"],
    )
    restored = CriticalSet.model_validate(cs.model_dump())
    assert restored == cs
    assert CriticalSet.model_validate_json(cs.model_dump_json()) == cs


def test_critical_set_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        CriticalSet.model_validate({"unexpected": 1})


# --- Claim / GroundedSummary ----------------------------------------------


def test_claim_with_sources_is_grounded() -> None:
    claim = Claim(text="A1c is 7.1%", sources=[SourceRef(resource_type="Observation", id="o-1")])
    assert claim.is_grounded is True


def test_claim_without_sources_is_constructible_but_ungrounded() -> None:
    """An empty-sources claim parses but is flagged as not a grounded fact."""
    claim = Claim(text="unsupported statement", sources=[])
    assert claim.sources == []
    assert claim.is_grounded is False


def test_claim_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        Claim(text="", sources=[])


def test_grounded_summary_parses_and_round_trips() -> None:
    summary = GroundedSummary(
        headline="Diabetic patient, controlled",
        must_knows=[
            Claim(text="On metformin", sources=[SourceRef(resource_type="Medication", id="m-1")])
        ],
        whats_changed=[
            Claim(text="A1c improved", sources=[SourceRef(resource_type="Observation", id="o-1")])
        ],
        caveats=["No recent lipid panel"],
    )
    restored = GroundedSummary.model_validate(summary.model_dump())
    assert restored == summary
    assert summary.must_knows[0].is_grounded is True


def test_grounded_summary_rejects_empty_headline() -> None:
    with pytest.raises(ValidationError):
        GroundedSummary(headline="")


def test_grounded_summary_rejects_malformed_claim() -> None:
    with pytest.raises(ValidationError):
        GroundedSummary.model_validate({"headline": "h", "must_knows": [{"sources": []}]})
