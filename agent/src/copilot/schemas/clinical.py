"""Clinical domain contracts for the M1 retrieval tools (FR-1..FR-5, FR-11).

Every clinical record carries a stable `source_id` + `timestamp` via its
`source: SourceRef`, so each claim the agent surfaces can be grounded and the
UI can link back to the originating FHIR record (PRD §11). These models are the
**source of truth** (NFR-3): retrieval tools return them and malformed upstream
data is rejected here at the schema layer rather than downstream.

Design notes mirror `schemas/core.py`:
- Pydantic v2 throughout.
- Value objects are `frozen` so they behave as immutable identities once built.
- `extra="forbid"` everywhere: an unexpected key is a contract violation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field

from copilot.schemas.core import SourceRef

__all__ = [
    "ScheduledPatient",
    "PanelDecision",
    "Medication",
    "Allergy",
    "LabResult",
    "Problem",
    "Encounter",
    "Deltas",
    "CriticalSet",
]


class ScheduledPatient(BaseModel):
    """A patient on the clinician's schedule for the day (FR-1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    patient_id: str = Field(min_length=1, description="FHIR Patient resource id.")
    name: str = Field(min_length=1, description="Display name.")
    start: datetime = Field(description="Appointment start time.")
    appointment_id: str = Field(min_length=1, description="FHIR Appointment resource id.")
    source: SourceRef = Field(description="Pointer to the source record (grounding).")


class PanelDecision(BaseModel):
    """Whether a patient is in the clinician's panel, and why (FR-2).

    `break_glass` records that access was granted despite the patient falling
    outside the panel, so the override is auditable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    in_panel: bool = Field(description="Whether the patient is in the clinician's panel.")
    reason: str = Field(min_length=1, description="Human-readable basis for the decision.")
    break_glass: bool = Field(
        default=False,
        description="True when panel scoping was overridden to grant access.",
    )
    source: SourceRef | None = Field(
        default=None,
        description="Pointer to the record backing the decision, if any.",
    )


class Medication(BaseModel):
    """A medication record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="FHIR MedicationRequest/Statement resource id.")
    name: str = Field(min_length=1, description="Medication display name.")
    status: str = Field(min_length=1, description="Medication status, e.g. 'active'.")
    dosage: str | None = Field(default=None, description="Dosage instruction text, if known.")
    source: SourceRef = Field(description="Pointer to the source record (grounding).")


class Allergy(BaseModel):
    """An allergy / intolerance record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="FHIR AllergyIntolerance resource id.")
    substance: str = Field(min_length=1, description="Substance the patient reacts to.")
    reaction: str | None = Field(default=None, description="Reaction manifestation, if known.")
    criticality: str | None = Field(default=None, description="Criticality, e.g. 'high'.")
    source: SourceRef = Field(description="Pointer to the source record (grounding).")


class LabResult(BaseModel):
    """A laboratory observation / result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="FHIR Observation resource id.")
    name: str = Field(min_length=1, description="Test / analyte display name.")
    value: str | None = Field(default=None, description="Result value as text, if known.")
    unit: str | None = Field(default=None, description="Unit of measure, if known.")
    effective: datetime | None = Field(
        default=None,
        description="Clinically effective time of the result, if known.",
    )
    abnormal: bool | None = Field(
        default=None,
        description="Whether the result is flagged abnormal, if known.",
    )
    source: SourceRef = Field(description="Pointer to the source record (grounding).")


class Problem(BaseModel):
    """A problem-list / condition entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="FHIR Condition resource id.")
    name: str = Field(min_length=1, description="Problem / condition display name.")
    clinical_status: str | None = Field(
        default=None,
        description="Clinical status, e.g. 'active', if known.",
    )
    onset: date | None = Field(default=None, description="Onset date, if known.")
    source: SourceRef = Field(description="Pointer to the source record (grounding).")


class Encounter(BaseModel):
    """An encounter / visit record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="FHIR Encounter resource id.")
    kind: str | None = Field(default=None, description="Encounter class / type, if known.")
    start: datetime | None = Field(default=None, description="Encounter start time, if known.")
    source: SourceRef = Field(description="Pointer to the source record (grounding).")


class Deltas(BaseModel):
    """What changed since a reference visit (FR-5).

    Each list holds full record objects (not just ids) so the caller can render
    and ground a "what's changed" view without a second lookup.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_visit: datetime | None = Field(
        default=None,
        description="The visit the deltas are computed against, if known.",
    )
    new_meds: list[Medication] = Field(
        default_factory=list,
        description="Medications started since the reference visit.",
    )
    stopped_meds: list[Medication] = Field(
        default_factory=list,
        description="Medications stopped since the reference visit.",
    )
    new_problems: list[Problem] = Field(
        default_factory=list,
        description="Problems added since the reference visit.",
    )
    new_labs: list[LabResult] = Field(
        default_factory=list,
        description="Lab results reported since the reference visit.",
    )
    new_encounters: list[Encounter] = Field(
        default_factory=list,
        description="Encounters since the reference visit.",
    )


class CriticalSet(BaseModel):
    """The tiered "must-know" clinical bundle for a patient (FR-4, FR-11).

    Aggregates the highest-priority clinical facts. `missing` names any tier
    that could not be retrieved, so a partial bundle degrades gracefully rather
    than crashing (the same contract as `ToolResult.missing`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    medications: list[Medication] = Field(
        default_factory=list,
        description="Active / relevant medications.",
    )
    allergies: list[Allergy] = Field(
        default_factory=list,
        description="Known allergies and intolerances.",
    )
    labs: list[LabResult] = Field(
        default_factory=list,
        description="Recent / relevant lab results.",
    )
    problems: list[Problem] = Field(
        default_factory=list,
        description="Active / relevant problems.",
    )
    deltas: Deltas | None = Field(
        default=None,
        description="What changed since the reference visit, if computed.",
    )
    retrieved_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the bundle was assembled.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="Names of the tiers that could not be retrieved.",
    )
    labs_omitted: int = Field(
        default=0,
        description="Lab records available but not analysed (bounded away); 0 when full labs were analysed.",
    )
