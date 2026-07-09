"""Unit tests for the hand-rolled orchestrator (PRP M1-7).

Exercises the full M1 lifecycle — gate → parallel retrieval → synthesis →
verification → envelope — with **both** the LLM and FHIR mocked, so no
ANTHROPIC_API_KEY and no live OpenEMR stack are needed:

* a paneled patient produces a verified, grounded envelope; ungrounded model
  claims are dropped by the gate;
* an out-of-panel patient is refused **before any clinical read** (the fake
  FHIR client records that only the gate resources were touched);
* a retrieval tier that fails is surfaced in ``missing`` (FR-11/FR-12);
* an LLM failure degrades to a no-summary envelope naming ``summary`` in
  ``missing`` rather than raising.
"""

from __future__ import annotations

from typing import Any

from copilot.llm.client import LLMError
from copilot.openemr.client import FhirError
from copilot.orchestrator.controller import HandRolledOrchestrator, PatientSummary
from copilot.schemas.clinical import CriticalSet, Deltas
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

PATIENT = "p1"
PROVIDER = "admin"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFhirClient:
    """A FhirClient stand-in returning canned Bundles keyed by path.

    ``get`` records every ``(path, params)`` so a test can prove the gate ran
    *before* any clinical retrieval (an out-of-panel request must never touch a
    clinical resource path). A path may map to a callable to raise instead.
    """

    def __init__(self, bundles: dict[str, Any]) -> None:
        self._bundles = bundles
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        value = self._bundles.get(path)
        if callable(value):
            return value()
        if value is None:
            return {"resourceType": "Bundle", "entry": []}
        return value

    @property
    def touched_paths(self) -> set[str]:
        return {path for path, _ in self.calls}


class FakeLLM:
    """An LLMClient stand-in: returns a preset summary or raises a preset error."""

    def __init__(
        self,
        summary: GroundedSummary | None = None,
        *,
        error: LLMError | None = None,
    ) -> None:
        self._summary = summary
        self._error = error
        self.calls: list[tuple[CriticalSet, Deltas]] = []

    async def summarize(self, critical_set: CriticalSet, deltas: Deltas) -> GroundedSummary:
        self.calls.append((critical_set, deltas))
        if self._error is not None:
            raise self._error
        assert self._summary is not None
        return self._summary


# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "entry": [{"resource": r} for r in resources],
    }


def _appointment(patient_id: str) -> dict[str, Any]:
    return {
        "resourceType": "Appointment",
        "id": "appt-1",
        "start": "2026-07-07T09:00:00Z",
        "participant": [
            {"actor": {"reference": f"Patient/{patient_id}", "display": "Jane Roe"}}
        ],
    }


def _medication() -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": "med-1",
        "status": "active",
        "authoredOn": "2026-06-01T00:00:00Z",
        "medicationCodeableConcept": {"text": "Lisinopril"},
    }


def _condition() -> dict[str, Any]:
    return {
        "resourceType": "Condition",
        "id": "cond-1",
        "code": {"text": "Hypertension"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
    }


def _encounter(eid: str, start: str) -> dict[str, Any]:
    return {
        "resourceType": "Encounter",
        "id": eid,
        "class": {"code": "AMB"},
        "period": {"start": start},
    }


def _in_panel_bundles() -> dict[str, Any]:
    """A patient on today's schedule with a small, retrievable chart."""

    return {
        "/Appointment": _bundle(_appointment(PATIENT)),
        "/MedicationRequest": _bundle(_medication()),
        "/AllergyIntolerance": _bundle(),
        "/Observation": _bundle(),
        "/Condition": _bundle(_condition()),
        # Two dated encounters so deltas has a reference visit to diff against.
        "/Encounter": _bundle(
            _encounter("enc-2", "2026-07-01T09:00:00Z"),
            _encounter("enc-1", "2026-05-01T09:00:00Z"),
        ),
    }


def _grounded_summary() -> GroundedSummary:
    """A summary with one grounded must-know (cites med-1) and one ungrounded one."""

    return GroundedSummary(
        headline="Stable hypertensive on lisinopril.",
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
        whats_changed=[],
        caveats=["Allergy list unremarkable."],
    )


# ---------------------------------------------------------------------------
# In-panel: grounded, verified envelope
# ---------------------------------------------------------------------------


async def test_paneled_patient_returns_verified_grounded_summary() -> None:
    fhir = FakeFhirClient(_in_panel_bundles())
    llm = FakeLLM(_grounded_summary())
    orch = HandRolledOrchestrator(fhir_client=fhir, llm_client=llm)  # type: ignore[arg-type]

    result = await orch.patient_summary(PATIENT, PROVIDER)

    assert isinstance(result, PatientSummary)
    assert result.decision.in_panel is True
    assert result.refused is False
    assert result.verified is not None
    # The LLM was called with the retrieved records.
    assert llm.calls, "expected the LLM synthesis step to run"

    # Grounding gate: the ungrounded 'phantom drug' claim is dropped.
    kept = result.verified.summary.must_knows
    assert len(kept) == 1
    assert kept[0].sources[0].id == "med-1"
    assert any(c.sources[0].id == "ghost" for c in result.verified.dropped)

    # A data timestamp is available for the "data as of" line.
    assert result.data_as_of is not None


# ---------------------------------------------------------------------------
# Out-of-panel: refused BEFORE any clinical read
# ---------------------------------------------------------------------------


async def test_out_of_panel_patient_is_refused_before_retrieval() -> None:
    # Empty schedule and empty fallback encounter search → out of panel.
    fhir = FakeFhirClient({"/Appointment": _bundle(), "/Encounter": _bundle()})
    llm = FakeLLM(_grounded_summary())
    orch = HandRolledOrchestrator(fhir_client=fhir, llm_client=llm)  # type: ignore[arg-type]

    result = await orch.patient_summary("stranger", PROVIDER)

    assert result.decision.in_panel is False
    assert result.refused is True
    assert result.verified is None
    assert llm.calls == [], "LLM must not run for an out-of-panel patient"

    # No clinical resource was ever fetched — only the gate's Appointment /
    # Encounter lookups.
    clinical = {"/MedicationRequest", "/AllergyIntolerance", "/Observation", "/Condition"}
    assert not (fhir.touched_paths & clinical)


# ---------------------------------------------------------------------------
# Graceful degradation (FR-11 / FR-12)
# ---------------------------------------------------------------------------


async def test_failed_tier_is_reported_in_missing() -> None:
    bundles = _in_panel_bundles()

    def _boom() -> dict[str, Any]:
        raise FhirError("simulated allergy index outage", status_code=503, retriable=False)

    bundles["/AllergyIntolerance"] = _boom
    fhir = FakeFhirClient(bundles)
    llm = FakeLLM(_grounded_summary())
    orch = HandRolledOrchestrator(fhir_client=fhir, llm_client=llm)  # type: ignore[arg-type]

    result = await orch.patient_summary(PATIENT, PROVIDER)

    assert "allergies" in result.missing
    # The rest of the summary still came through.
    assert result.verified is not None


async def test_llm_failure_degrades_to_no_summary() -> None:
    fhir = FakeFhirClient(_in_panel_bundles())
    llm = FakeLLM(error=LLMError("model refused", retriable=False))
    orch = HandRolledOrchestrator(fhir_client=fhir, llm_client=llm)  # type: ignore[arg-type]

    result = await orch.patient_summary(PATIENT, PROVIDER)

    # Access was granted and retrieval ran, but no trustworthy summary exists.
    assert result.decision.in_panel is True
    assert result.verified is None
    assert "summary" in result.missing
    assert result.data_as_of is not None


# ---------------------------------------------------------------------------
# Break-glass override
# ---------------------------------------------------------------------------


async def test_break_glass_grants_access_out_of_panel() -> None:
    fhir = FakeFhirClient(_in_panel_bundles() | {"/Appointment": _bundle()})
    llm = FakeLLM(_grounded_summary())
    orch = HandRolledOrchestrator(fhir_client=fhir, llm_client=llm)  # type: ignore[arg-type]

    result = await orch.patient_summary(
        PATIENT, PROVIDER, break_glass_reason="covering colleague on call"
    )

    assert result.decision.in_panel is True
    assert result.decision.break_glass is True
    assert result.verified is not None
