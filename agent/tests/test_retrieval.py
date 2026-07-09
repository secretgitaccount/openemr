"""Unit tests for tiered parallel critical-set retrieval (PRP M1-3, FR-3/4/11).

OpenEMR is mocked with ``respx`` (the async httpx transport is intercepted); the
live end-to-end proof is the ``python -m copilot.openemr.retrieval --patient
<id>`` gate in the PRP's Validation. No Anthropic API key is required.

Coverage:

* FHIR search Bundles → typed, source-bound M1-1 records for all four tiers;
* the FR-11 crux: an empty allergy search is "no known" (``partial=False``,
  ``missing=[]``), a *failed* allergy fetch is "not retrieved"
  (``partial=True``, ``missing=["allergies"]``);
* ``get_recent_labs`` flags ``abnormal`` from interpretation and reference
  range, and bounds the search with ``date=ge<since>``;
* ``get_critical_set`` assembles a full bundle with sources, and — on a single
  tier failing — returns a *partial* set (that tier in ``missing``) with the
  other three tiers still populated, never raising.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx

from copilot.config import Settings
from copilot.openemr.client import FhirClient
from copilot.openemr.retrieval import (
    get_active_medications,
    get_allergies,
    get_critical_set,
    get_problem_list,
    get_recent_labs,
)
from copilot.schemas.clinical import Allergy, LabResult, Medication, Problem
from copilot.schemas.core import ToolResult

FHIR_BASE = "http://oemr.test/apis/default/fhir"
PATIENT = "pat-1"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openemr_base_url="http://oemr.test",
        openemr_fhir_base=FHIR_BASE,
        openemr_oauth_base="http://oemr.test/oauth2/default",
    )


class _StubTokens:
    """Minimal ``TokenSource``: returns a fixed access token."""

    def get_access_token(self) -> str:
        return "user-access-token"


def _bundle(*resources: dict) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


# --- sample resources ------------------------------------------------------


def _medication(**overrides: object) -> dict:
    r = {
        "resourceType": "MedicationRequest",
        "id": "med-1",
        "status": "active",
        "authoredOn": "2026-05-01T10:00:00Z",
        "medicationCodeableConcept": {"text": "Lisinopril 10 mg tablet"},
        "dosageInstruction": [{"text": "1 tablet daily"}],
    }
    r.update(overrides)
    return r


def _allergy(**overrides: object) -> dict:
    r = {
        "resourceType": "AllergyIntolerance",
        "id": "alg-1",
        "criticality": "high",
        "recordedDate": "2025-01-02T00:00:00Z",
        "code": {"text": "Penicillin"},
        "reaction": [{"manifestation": [{"text": "Hives"}]}],
    }
    r.update(overrides)
    return r


def _lab(**overrides: object) -> dict:
    r = {
        "resourceType": "Observation",
        "id": "lab-1",
        "status": "final",
        "category": [{"coding": [{"code": "laboratory"}]}],
        "code": {"text": "Potassium"},
        "effectiveDateTime": "2026-06-15T08:30:00Z",
        "valueQuantity": {"value": 5.9, "unit": "mmol/L"},
        "interpretation": [{"coding": [{"code": "H"}]}],
    }
    r.update(overrides)
    return r


def _problem(**overrides: object) -> dict:
    r = {
        "resourceType": "Condition",
        "id": "cond-1",
        "recordedDate": "2024-03-04T00:00:00Z",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"text": "Type 2 diabetes mellitus"},
        "onsetDateTime": "2024-03-01",
    }
    r.update(overrides)
    return r


def _fhir_client(settings: Settings) -> FhirClient:
    return FhirClient(_StubTokens(), settings=settings)


# ---------------------------------------------------------------------------
# Per-tier mapping
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_active_medications_maps_and_grounds(settings: Settings) -> None:
    route = respx.get(f"{FHIR_BASE}/MedicationRequest").mock(
        return_value=httpx.Response(200, json=_bundle(_medication()))
    )
    async with _fhir_client(settings) as client:
        result = await get_active_medications(PATIENT, client=client)

    assert isinstance(result, ToolResult)
    assert result.partial is False and result.missing == []
    (med,) = result.data
    assert isinstance(med, Medication)
    assert med.name == "Lisinopril 10 mg tablet"
    assert med.status == "active"
    assert med.dosage == "1 tablet daily"
    assert med.source.resource_type == "MedicationRequest"
    assert med.source.id == "med-1"
    assert med.source.timestamp == datetime.fromisoformat("2026-05-01T10:00:00+00:00")
    assert result.sources == [med.source]
    # user token + active-status filter were used
    request = route.calls.last.request
    assert request.url.params["status"] == "active"
    assert request.url.params["patient"] == PATIENT


@respx.mock
async def test_get_allergies_maps_reaction_and_criticality(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/AllergyIntolerance").mock(
        return_value=httpx.Response(200, json=_bundle(_allergy()))
    )
    async with _fhir_client(settings) as client:
        result = await get_allergies(PATIENT, client=client)

    (allergy,) = result.data
    assert isinstance(allergy, Allergy)
    assert allergy.substance == "Penicillin"
    assert allergy.reaction == "Hives"
    assert allergy.criticality == "high"
    assert allergy.source.resource_type == "AllergyIntolerance"


@respx.mock
async def test_get_problem_list_maps_status_and_onset(settings: Settings) -> None:
    route = respx.get(f"{FHIR_BASE}/Condition").mock(
        return_value=httpx.Response(200, json=_bundle(_problem()))
    )
    async with _fhir_client(settings) as client:
        result = await get_problem_list(PATIENT, client=client)

    (problem,) = result.data
    assert isinstance(problem, Problem)
    assert problem.name == "Type 2 diabetes mellitus"
    assert problem.clinical_status == "active"
    assert problem.onset == date(2024, 3, 1)
    assert route.calls.last.request.url.params["clinical-status"] == "active"


# ---------------------------------------------------------------------------
# Labs: abnormal detection + since bound
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_recent_labs_flags_abnormal_from_interpretation(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle(_lab()))
    )
    async with _fhir_client(settings) as client:
        result = await get_recent_labs(PATIENT, client=client)

    (lab,) = result.data
    assert isinstance(lab, LabResult)
    assert lab.name == "Potassium"
    assert lab.value == "5.9"
    assert lab.unit == "mmol/L"
    assert lab.abnormal is True
    assert lab.effective == datetime.fromisoformat("2026-06-15T08:30:00+00:00")


@respx.mock
async def test_get_recent_labs_flags_abnormal_from_reference_range(settings: Settings) -> None:
    lab = _lab()
    del lab["interpretation"]
    lab["referenceRange"] = [{"low": {"value": 3.5}, "high": {"value": 5.1}}]
    respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle(lab))
    )
    async with _fhir_client(settings) as client:
        result = await get_recent_labs(PATIENT, client=client)

    assert result.data[0].abnormal is True  # 5.9 > 5.1


@respx.mock
async def test_get_recent_labs_normal_within_range(settings: Settings) -> None:
    lab = _lab(valueQuantity={"value": 4.2, "unit": "mmol/L"})
    del lab["interpretation"]
    lab["referenceRange"] = [{"low": {"value": 3.5}, "high": {"value": 5.1}}]
    respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle(lab))
    )
    async with _fhir_client(settings) as client:
        result = await get_recent_labs(PATIENT, client=client)

    assert result.data[0].abnormal is False


@respx.mock
async def test_get_recent_labs_since_bounds_search(settings: Settings) -> None:
    route = respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle())
    )
    async with _fhir_client(settings) as client:
        await get_recent_labs(PATIENT, since=date(2026, 1, 1), client=client)

    params = route.calls.last.request.url.params
    assert params["category"] == "laboratory"
    assert params["date"] == "ge2026-01-01"


# ---------------------------------------------------------------------------
# FR-11 crux: "no known" vs "not retrieved"
# ---------------------------------------------------------------------------


@respx.mock
async def test_empty_allergies_means_no_known_not_missing(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/AllergyIntolerance").mock(
        return_value=httpx.Response(200, json=_bundle())
    )
    async with _fhir_client(settings) as client:
        result = await get_allergies(PATIENT, client=client)

    # No data on file — an empty but successful result.
    assert result.data == []
    assert result.partial is False
    assert result.missing == []


@respx.mock
async def test_failed_allergy_fetch_is_partial_and_missing(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/AllergyIntolerance").mock(
        return_value=httpx.Response(500, json={"resourceType": "OperationOutcome"})
    )
    async with _fhir_client(settings) as client:
        result = await get_allergies(PATIENT, client=client)

    # Not retrieved — distinct from "no known".
    assert result.data == []
    assert result.partial is True
    assert result.missing == ["allergies"]


# ---------------------------------------------------------------------------
# Parallel assembly
# ---------------------------------------------------------------------------


def _mock_all_ok() -> None:
    respx.get(f"{FHIR_BASE}/MedicationRequest").mock(
        return_value=httpx.Response(200, json=_bundle(_medication()))
    )
    respx.get(f"{FHIR_BASE}/AllergyIntolerance").mock(
        return_value=httpx.Response(200, json=_bundle(_allergy()))
    )
    respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle(_lab()))
    )
    respx.get(f"{FHIR_BASE}/Condition").mock(
        return_value=httpx.Response(200, json=_bundle(_problem()))
    )


@respx.mock
async def test_get_critical_set_assembles_all_tiers(settings: Settings) -> None:
    _mock_all_ok()
    async with _fhir_client(settings) as client:
        cset = await get_critical_set(PATIENT, client=client)

    assert [m.name for m in cset.medications] == ["Lisinopril 10 mg tablet"]
    assert [a.substance for a in cset.allergies] == ["Penicillin"]
    assert [x.name for x in cset.labs] == ["Potassium"]
    assert [p.name for p in cset.problems] == ["Type 2 diabetes mellitus"]
    assert cset.missing == []
    # every surfaced record is source-bound (grounding, FR-8)
    assert cset.medications[0].source.id == "med-1"
    assert cset.labs[0].source.resource_type == "Observation"


@respx.mock
async def test_get_critical_set_degrades_on_single_tier_failure(settings: Settings) -> None:
    # Labs fail; the other three tiers still return.
    respx.get(f"{FHIR_BASE}/MedicationRequest").mock(
        return_value=httpx.Response(200, json=_bundle(_medication()))
    )
    respx.get(f"{FHIR_BASE}/AllergyIntolerance").mock(
        return_value=httpx.Response(200, json=_bundle(_allergy()))
    )
    respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(503, json={"resourceType": "OperationOutcome"})
    )
    respx.get(f"{FHIR_BASE}/Condition").mock(
        return_value=httpx.Response(200, json=_bundle(_problem()))
    )

    async with _fhir_client(settings) as client:
        cset = await get_critical_set(PATIENT, client=client)  # must not raise

    assert cset.missing == ["labs"]
    assert cset.labs == []
    assert len(cset.medications) == 1
    assert len(cset.allergies) == 1
    assert len(cset.problems) == 1


@respx.mock
async def test_get_critical_set_never_raises_on_raised_tool(settings: Settings, monkeypatch) -> None:
    # A tool that raises outright (not a caught FhirError) is still folded into
    # `missing` via gather(return_exceptions=True), not propagated.
    import copilot.openemr.retrieval as retrieval

    respx.get(f"{FHIR_BASE}/AllergyIntolerance").mock(
        return_value=httpx.Response(200, json=_bundle(_allergy()))
    )
    respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle(_lab()))
    )
    respx.get(f"{FHIR_BASE}/Condition").mock(
        return_value=httpx.Response(200, json=_bundle(_problem()))
    )

    async def _boom(patient_id: str, *, client: FhirClient) -> ToolResult:
        raise RuntimeError("unexpected mapping bug")

    monkeypatch.setattr(retrieval, "get_active_medications", _boom)

    async with _fhir_client(settings) as client:
        cset = await get_critical_set(PATIENT, client=client)

    assert "medications" in cset.missing
    assert cset.medications == []
    assert len(cset.allergies) == 1
