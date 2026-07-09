"""Invariant eval cases (PRD §13) — properties that must hold on every request.

Each case pins an *invariant* of the trust boundary and names, in its docstring,
the failure mode it guards. Deterministic and offline (FakeLLM + FakeFhirClient),
so the suite is free and needs no key.
"""

from __future__ import annotations

from copilot.openemr.retrieval import get_critical_set
from copilot.orchestrator.controller import HandRolledOrchestrator
from copilot.schemas.clinical import CriticalSet, Medication
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary
from copilot.verification.gate import _valid_refs, verify

from _helpers import (
    PATIENT,
    PROVIDER,
    FakeFhirClient,
    FakeLLM,
    bundle,
    in_panel_bundles,
    medication,
)

_CLINICAL_PATHS = {"/MedicationRequest", "/AllergyIntolerance", "/Observation", "/Condition"}


def _summary_with_phantom() -> GroundedSummary:
    """One grounded must-know (cites med-1) plus one that cites a record not
    present in the chart — the gate must strip the latter."""

    return GroundedSummary(
        headline="Hypertensive on lisinopril.",
        must_knows=[
            Claim(
                text="Active on lisinopril.",
                sources=[SourceRef(resource_type="MedicationRequest", id="med-1")],
            ),
            Claim(
                text="On a phantom drug not in the chart.",
                sources=[SourceRef(resource_type="MedicationRequest", id="ghost")],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Every rendered claim carries at least one *valid* source id.
# ---------------------------------------------------------------------------


async def test_every_rendered_claim_carries_a_valid_source() -> None:
    """Guards: a rendered claim reaching the clinician with no citation, or one
    citing a record that was never retrieved — an ungrounded fact shown as fact."""

    llm = FakeLLM(summary=_summary_with_phantom())
    orch = HandRolledOrchestrator(
        fhir_client=FakeFhirClient(in_panel_bundles(meds=[medication("med-1", "Lisinopril")])),  # type: ignore[arg-type]
        llm_client=llm,  # type: ignore[arg-type]
    )

    result = await orch.patient_summary(PATIENT, PROVIDER)
    assert result.verified is not None

    # Reconstruct the ground-truth reference set the gate verified against.
    critical_set, deltas = llm.summarize_calls[0]
    valid = _valid_refs(critical_set.model_copy(update={"deltas": deltas}))

    rendered = [*result.verified.summary.must_knows, *result.verified.summary.whats_changed]
    assert rendered, "expected at least one grounded claim to render"
    for claim in rendered:
        assert claim.sources, "a rendered claim must carry >=1 source"
        for source in claim.sources:
            assert (source.resource_type, source.id) in valid


# ---------------------------------------------------------------------------
# An ungrounded claim is always dropped — never shown as fact.
# ---------------------------------------------------------------------------


def test_ungrounded_claim_is_always_dropped() -> None:
    """Guards: a model-invented claim (sourceless, or citing an absent record)
    surfacing as a fact instead of being dropped at the gate."""

    med = Medication(
        id="m1",
        name="Lisinopril",
        status="active",
        source=SourceRef(resource_type="MedicationRequest", id="m1"),
    )
    critical_set = CriticalSet(medications=[med])

    grounded = Claim(text="On lisinopril", sources=[med.source])
    sourceless = Claim(text="Unsupported assertion")
    fabricated = Claim(
        text="On a phantom drug",
        sources=[SourceRef(resource_type="MedicationRequest", id="ghost")],
    )
    summary = GroundedSummary(headline="H", must_knows=[grounded, sourceless, fabricated])

    result = verify(summary, critical_set)

    assert result.summary.must_knows == [grounded]
    assert sourceless in result.dropped
    assert fabricated in result.dropped


# ---------------------------------------------------------------------------
# Absence != negative finding: "no data on file" vs "no known".
# ---------------------------------------------------------------------------


async def test_absence_is_distinct_from_negative_finding() -> None:
    """Guards: telling the clinician "no known allergies" when the allergy tier
    was never retrieved — an unretrieved tier must stay distinguishable from an
    empty one."""

    # Retrieved-but-empty → "no known allergies": empty list, not in `missing`.
    empty = await get_critical_set(
        PATIENT, client=FakeFhirClient({"/AllergyIntolerance": bundle()})  # type: ignore[arg-type]
    )
    assert empty.allergies == []
    assert "allergies" not in empty.missing

    # Not-retrieved → "no data on file": still empty list, but named in `missing`.
    def _boom() -> dict:
        from copilot.openemr.client import FhirError

        raise FhirError("allergy index down", status_code=503, retriable=False)

    unretrieved = await get_critical_set(
        PATIENT, client=FakeFhirClient({"/AllergyIntolerance": _boom})  # type: ignore[arg-type]
    )
    assert unretrieved.allergies == []
    assert "allergies" in unretrieved.missing

    # The two empty-list states are only telling apart via `missing`.
    assert ("allergies" in empty.missing) != ("allergies" in unretrieved.missing)


# ---------------------------------------------------------------------------
# The gate always runs before any retrieval — no read on refusal.
# ---------------------------------------------------------------------------


async def test_panel_gate_runs_before_any_clinical_read() -> None:
    """Guards: a clinical resource being read for an out-of-panel patient — the
    panel gate must short-circuit ahead of every clinical fetch."""

    # Empty schedule + empty encounter fallback → out of panel.
    fhir = FakeFhirClient({"/Appointment": bundle(), "/Encounter": bundle()})
    orch = HandRolledOrchestrator(
        fhir_client=fhir,  # type: ignore[arg-type]
        llm_client=FakeLLM(summary=_summary_with_phantom()),  # type: ignore[arg-type]
    )

    result = await orch.patient_summary("stranger", PROVIDER)

    assert result.refused is True
    assert result.verified is None
    # Not one clinical resource path was touched before the refusal.
    assert not (fhir.touched_paths & _CLINICAL_PATHS)
