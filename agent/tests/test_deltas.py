"""Unit tests for delta-since-last-visit computation (PRP M1-4, FR-5, UC-1).

OpenEMR is mocked with ``respx`` (the async httpx transport is intercepted); the
live proof is the ``python -m copilot.openemr.deltas --patient <id>`` gate in the
PRP's Validation. No Anthropic API key is required.

Coverage:

* reference-point selection — the visit **before** the most recent one is the
  anchor, and windowing keeps only meds/problems/labs/encounters on/after it;
* med windowing splits into new (started) vs stopped (terminal status);
* a patient with fewer than two dated encounters yields an **empty but valid**
  :class:`Deltas` (``reference_visit=None``), never an error;
* a failed **encounter history** fetch degrades to an empty ``Deltas`` flagged
  ``partial=True`` / ``missing=["deltas"]`` (FR-11);
* a failed **sub-tier** fetch (meds) folds that tier into ``missing`` while the
  rest of the diff still computes.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import respx

from copilot.config import Settings
from copilot.openemr.client import FhirClient
from copilot.openemr.deltas import get_deltas_since_last_visit, get_encounters_since
from copilot.schemas.clinical import Deltas, Encounter
from copilot.schemas.core import ToolResult

FHIR_BASE = "http://oemr.test/apis/default/fhir"
PATIENT = "pat-1"

# Reference anchor: the second-most-recent visit is 2026-03-01. Anything on/after
# it is "new since last visit"; anything before it is prior history.
_REFERENCE = datetime.fromisoformat("2026-03-01T09:00:00+00:00")


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


def _fhir_client(settings: Settings) -> FhirClient:
    return FhirClient(_StubTokens(), settings=settings)


def _bundle(*resources: dict) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


# --- sample resources ------------------------------------------------------


def _encounter(enc_id: str, start: str, **overrides: object) -> dict:
    r: dict = {
        "resourceType": "Encounter",
        "id": enc_id,
        "status": "finished",
        "class": {"code": "AMB", "display": "ambulatory"},
        "period": {"start": start},
    }
    r.update(overrides)
    return r


def _medication(med_id: str, authored_on: str, status: str = "active", **overrides: object) -> dict:
    r: dict = {
        "resourceType": "MedicationRequest",
        "id": med_id,
        "status": status,
        "authoredOn": authored_on,
        "medicationCodeableConcept": {"text": f"Drug {med_id}"},
    }
    r.update(overrides)
    return r


def _problem(cond_id: str, recorded: str, **overrides: object) -> dict:
    r: dict = {
        "resourceType": "Condition",
        "id": cond_id,
        "recordedDate": recorded,
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"text": f"Problem {cond_id}"},
    }
    r.update(overrides)
    return r


def _lab(lab_id: str, effective: str, **overrides: object) -> dict:
    r: dict = {
        "resourceType": "Observation",
        "id": lab_id,
        "status": "final",
        "category": [{"coding": [{"code": "laboratory"}]}],
        "code": {"text": f"Analyte {lab_id}"},
        "effectiveDateTime": effective,
        "valueQuantity": {"value": 1.0, "unit": "mmol/L"},
    }
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------
# get_encounters_since
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_encounters_since_sorts_newest_first(settings: Settings) -> None:
    route = respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(
            200,
            json=_bundle(
                _encounter("enc-old", "2026-01-01T08:00:00Z"),
                _encounter("enc-new", "2026-06-01T08:00:00Z"),
                _encounter("enc-mid", "2026-03-01T09:00:00Z"),
            ),
        )
    )
    async with _fhir_client(settings) as client:
        result = await get_encounters_since(PATIENT, None, client=client)

    assert isinstance(result, ToolResult)
    assert result.partial is False
    assert [e.id for e in result.data] == ["enc-new", "enc-mid", "enc-old"]
    assert all(isinstance(e, Encounter) for e in result.data)
    assert result.data[0].kind == "ambulatory"
    # grounding: every encounter carries its source
    assert result.sources == [e.source for e in result.data]
    assert route.calls.last.request.url.params["patient"] == PATIENT
    assert route.calls.last.request.url.params["_sort"] == "-date"


@respx.mock
async def test_get_encounters_since_filters_by_since(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(
            200,
            json=_bundle(
                _encounter("enc-old", "2026-01-01T08:00:00Z"),
                _encounter("enc-new", "2026-06-01T08:00:00Z"),
            ),
        )
    )
    async with _fhir_client(settings) as client:
        result = await get_encounters_since(PATIENT, _REFERENCE, client=client)

    assert [e.id for e in result.data] == ["enc-new"]


@respx.mock
async def test_get_encounters_since_partial_on_fetch_failure(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(500, json={"resourceType": "OperationOutcome"})
    )
    async with _fhir_client(settings) as client:
        result = await get_encounters_since(PATIENT, None, client=client)

    assert result.data == []
    assert result.partial is True
    assert result.missing == ["deltas"]


# ---------------------------------------------------------------------------
# get_deltas_since_last_visit — reference point + windowing
# ---------------------------------------------------------------------------


def _mock_full_history() -> None:
    """Three visits (ref = 2026-03-01) plus meds/problems/labs straddling it."""

    respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(
            200,
            json=_bundle(
                _encounter("enc-1", "2026-01-01T08:00:00Z"),  # before reference
                _encounter("enc-2", "2026-03-01T09:00:00Z"),  # the reference visit
                _encounter("enc-3", "2026-06-01T08:00:00Z"),  # most recent (new)
            ),
        )
    )
    respx.get(f"{FHIR_BASE}/MedicationRequest").mock(
        return_value=httpx.Response(
            200,
            json=_bundle(
                _medication("med-old", "2026-02-01T00:00:00Z"),  # before ref -> excluded
                _medication("med-new", "2026-05-01T00:00:00Z"),  # after ref -> new
                _medication("med-stop", "2026-04-01T00:00:00Z", status="stopped"),  # stopped
            ),
        )
    )
    respx.get(f"{FHIR_BASE}/Condition").mock(
        return_value=httpx.Response(
            200,
            json=_bundle(
                _problem("cond-old", "2025-12-01T00:00:00Z"),  # before ref -> excluded
                _problem("cond-new", "2026-04-15T00:00:00Z"),  # after ref -> new
            ),
        )
    )
    respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle(_lab("lab-new", "2026-05-20T08:30:00Z")))
    )


@respx.mock
async def test_deltas_selects_reference_and_windows_changes(settings: Settings) -> None:
    _mock_full_history()
    async with _fhir_client(settings) as client:
        result = await get_deltas_since_last_visit(PATIENT, client=client)

    assert isinstance(result.data, Deltas)
    deltas = result.data
    assert result.partial is False
    assert result.missing == []

    # Reference is the second-most-recent visit, not the most recent.
    assert deltas.reference_visit == _REFERENCE

    assert [m.id for m in deltas.new_meds] == ["med-new"]
    assert [m.id for m in deltas.stopped_meds] == ["med-stop"]
    assert [p.id for p in deltas.new_problems] == ["cond-new"]
    assert [lab.id for lab in deltas.new_labs] == ["lab-new"]
    # Only the visit after the reference counts as a new encounter.
    assert [e.id for e in deltas.new_encounters] == ["enc-3"]


@respx.mock
async def test_deltas_labs_windowed_with_since_bound(settings: Settings) -> None:
    _mock_full_history()
    labs_route = respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle(_lab("lab-new", "2026-05-20T08:30:00Z")))
    )
    async with _fhir_client(settings) as client:
        await get_deltas_since_last_visit(PATIENT, client=client)

    params = labs_route.calls.last.request.url.params
    assert params["date"] == "ge2026-03-01"


@respx.mock
async def test_deltas_every_change_is_source_bound(settings: Settings) -> None:
    _mock_full_history()
    async with _fhir_client(settings) as client:
        result = await get_deltas_since_last_visit(PATIENT, client=client)

    # Grounding (FR-8): one SourceRef per changed record across every tier.
    changed = (
        result.data.new_meds
        + result.data.stopped_meds
        + result.data.new_problems
        + result.data.new_labs
        + result.data.new_encounters
    )
    assert len(result.sources) == len(changed)
    assert {s.id for s in result.sources} == {rec.source.id for rec in changed}


# ---------------------------------------------------------------------------
# Graceful degradation (FR-11)
# ---------------------------------------------------------------------------


@respx.mock
async def test_deltas_thin_history_is_empty_not_error(settings: Settings) -> None:
    # Only one dated encounter -> nothing to diff against.
    respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(
            200, json=_bundle(_encounter("enc-only", "2026-06-01T08:00:00Z"))
        )
    )
    async with _fhir_client(settings) as client:
        result = await get_deltas_since_last_visit(PATIENT, client=client)  # must not raise

    assert isinstance(result.data, Deltas)
    assert result.data.reference_visit is None
    assert result.data.new_meds == []
    assert result.data.new_encounters == []
    # A thin history is a valid answer, not a failure.
    assert result.partial is False
    assert result.missing == []


@respx.mock
async def test_deltas_history_fetch_failure_is_partial(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(500, json={"resourceType": "OperationOutcome"})
    )
    async with _fhir_client(settings) as client:
        result = await get_deltas_since_last_visit(PATIENT, client=client)

    assert isinstance(result.data, Deltas)
    assert result.data.reference_visit is None
    assert result.partial is True
    assert result.missing == ["deltas"]


@respx.mock
async def test_deltas_subtier_failure_folds_into_missing(settings: Settings) -> None:
    # Encounter history is fine, but the med fetch fails: the diff still computes
    # for the other tiers and flags "medications" in missing.
    respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(
            200,
            json=_bundle(
                _encounter("enc-2", "2026-03-01T09:00:00Z"),
                _encounter("enc-3", "2026-06-01T08:00:00Z"),
            ),
        )
    )
    respx.get(f"{FHIR_BASE}/MedicationRequest").mock(
        return_value=httpx.Response(503, json={"resourceType": "OperationOutcome"})
    )
    respx.get(f"{FHIR_BASE}/Condition").mock(
        return_value=httpx.Response(200, json=_bundle(_problem("cond-new", "2026-04-15T00:00:00Z")))
    )
    respx.get(f"{FHIR_BASE}/Observation").mock(
        return_value=httpx.Response(200, json=_bundle())
    )
    async with _fhir_client(settings) as client:
        result = await get_deltas_since_last_visit(PATIENT, client=client)

    assert result.partial is True
    assert "medications" in result.missing
    assert result.data.reference_visit == _REFERENCE
    assert result.data.new_meds == []
    assert [p.id for p in result.data.new_problems] == ["cond-new"]
