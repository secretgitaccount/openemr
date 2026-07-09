"""Boundary eval cases (PRD §13) — edges where a naive implementation degrades wrong.

Each case pins a *boundary* the point-of-care flow must handle exactly, and its
docstring names the failure mode it guards. Everything is deterministic: the LLM
is a :class:`FakeLLM` and OpenEMR a :class:`FakeFhirClient`, so the suite runs
green with no ``ANTHROPIC_API_KEY`` and no network.
"""

from __future__ import annotations

from copilot.openemr.client import FhirError
from copilot.openemr.deltas import get_deltas_since_last_visit
from copilot.openemr.retrieval import get_active_medications, get_allergies, get_critical_set
from copilot.orchestrator.controller import HandRolledOrchestrator
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

from _helpers import (
    PATIENT,
    PROVIDER,
    FakeFhirClient,
    FakeLLM,
    bundle,
    encounter,
    in_panel_bundles,
    medication,
)


def _grounded_summary() -> GroundedSummary:
    return GroundedSummary(
        headline="Stable hypertensive on lisinopril.",
        must_knows=[
            Claim(
                text="Active on lisinopril.",
                sources=[SourceRef(resource_type="MedicationRequest", id="med-1")],
            )
        ],
    )


# ---------------------------------------------------------------------------
# Missing tier: a retrieval failure surfaces as a notice, never a silent gap.
# ---------------------------------------------------------------------------


async def test_missing_tier_surfaces_as_notice_not_silent_gap() -> None:
    """Guards: a retrieval outage silently dropping a tier so the clinician can
    never tell the allergy list was *not checked* (vs genuinely empty)."""

    bundles = in_panel_bundles()

    def _boom() -> dict:
        raise FhirError("allergy index outage", status_code=503, retriable=False)

    bundles["/AllergyIntolerance"] = _boom
    orch = HandRolledOrchestrator(
        fhir_client=FakeFhirClient(bundles),  # type: ignore[arg-type]
        llm_client=FakeLLM(summary=_grounded_summary()),  # type: ignore[arg-type]
    )

    result = await orch.patient_summary(PATIENT, PROVIDER)

    # The un-retrieved tier is named for the FR-12 "could not retrieve X" notice…
    assert "allergies" in result.missing
    # …and the rest of the summary still came through (partial, not sunk).
    assert result.verified is not None


# ---------------------------------------------------------------------------
# Empty record: "no known allergies" (empty) is never conflated with "missing".
# ---------------------------------------------------------------------------


async def test_empty_allergies_is_no_known_not_missing() -> None:
    """Guards: rendering "none"/omitting the allergy line for an empty-but-
    retrieved list, indistinguishable from a tier that failed to load."""

    fhir = FakeFhirClient({"/AllergyIntolerance": bundle()})
    result = await get_allergies(PATIENT, client=fhir)  # type: ignore[arg-type]

    # Empty *and* successful → "no known allergies", not "not retrieved".
    assert result.data == []
    assert result.partial is False
    assert result.missing == []

    critical_set = await get_critical_set(PATIENT, client=fhir)  # type: ignore[arg-type]
    assert critical_set.allergies == []
    assert "allergies" not in critical_set.missing


# ---------------------------------------------------------------------------
# Thin history: <2 encounters yields empty deltas, not an error.
# ---------------------------------------------------------------------------


async def test_single_encounter_yields_empty_deltas_not_error() -> None:
    """Guards: a patient with fewer than two visits raising instead of returning
    a valid, empty "nothing changed" answer."""

    fhir = FakeFhirClient({"/Encounter": bundle(encounter("enc-1", "2026-05-01T09:00:00Z"))})

    result = await get_deltas_since_last_visit(PATIENT, client=fhir)  # type: ignore[arg-type]

    deltas = result.data
    assert deltas.reference_visit is None
    assert deltas.new_meds == []
    assert deltas.new_problems == []
    assert deltas.new_encounters == []
    # Thin history is a valid answer, not a failed fetch.
    assert result.partial is False
    assert result.missing == []


# ---------------------------------------------------------------------------
# Malformed FHIR: an uncontractable resource is skipped, not fatal.
# ---------------------------------------------------------------------------


async def test_malformed_resource_is_skipped_not_crash() -> None:
    """Guards: one half-valid upstream record (no id) crashing the whole fetch
    and taking every other valid medication down with it."""

    malformed = {"resourceType": "MedicationRequest", "status": "active"}  # no id
    no_name = {"resourceType": "MedicationRequest", "id": "med-x", "status": "active"}  # no code
    fhir = FakeFhirClient(
        {"/MedicationRequest": bundle(malformed, no_name, medication("med-1", "Lisinopril"))}
    )

    result = await get_active_medications(PATIENT, client=fhir)  # type: ignore[arg-type]

    # Only the fully-contractable record survives; the fetch did not raise.
    assert [m.id for m in result.data] == ["med-1"]
    assert result.partial is False
