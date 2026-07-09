"""Unit tests for the first authenticated FHIR call (PRP M0-7).

OpenEMR is mocked with ``respx`` here (the async httpx transport is intercepted
globally); the live end-to-end proof is the
``python -m copilot.openemr.tools --patient <id>`` gate in the PRP's Validation.

Coverage:

* FHIR ``Patient`` → :class:`Patient` mapping (name preference, gender, dob,
  grounding :class:`SourceRef` from ``meta.lastUpdated``);
* mapping rejects resources that can't satisfy the contract (wrong type, no
  name, missing birthDate) with a typed :class:`FhirError`;
* :func:`get_patient` returns a grounded :class:`ToolResult` end-to-end;
* the client sends the **user** bearer token *and* the ``X-Correlation-ID``
  header (so the read is borrowed-identity + traceable into ``api_log``);
* transient 5xx is retried then succeeds; a permanent 404 is not retried.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx

from copilot.config import Settings
from copilot.logging import (
    CORRELATION_ID_HEADER,
    reset_correlation_id,
    set_correlation_id,
)
from copilot.openemr.client import FhirClient, FhirError
from copilot.openemr.tools import get_patient, map_patient
from copilot.schemas.core import SourceRef, ToolResult
from copilot.schemas.patient import Patient

FHIR_BASE = "http://oemr.test/apis/default/fhir"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openemr_base_url="http://oemr.test",
        openemr_fhir_base=FHIR_BASE,
        openemr_oauth_base="http://oemr.test/oauth2/default",
    )


class _StubTokens:
    """Minimal ``TokenSource``: returns a fixed access token, counts calls."""

    def __init__(self, token: str = "user-access-token") -> None:
        self.token = token
        self.calls = 0

    def get_access_token(self) -> str:
        self.calls += 1
        return self.token


def _patient_resource(**overrides: object) -> dict:
    resource = {
        "resourceType": "Patient",
        "id": "abc-123",
        "meta": {"lastUpdated": "2026-07-09T06:08:02+00:00"},
        "name": [{"use": "official", "family": "Alpha", "given": ["Aaron", "Q"]}],
        "gender": "male",
        "birthDate": "1985-03-12",
    }
    resource.update(overrides)
    return resource


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_map_patient_happy_path() -> None:
    patient = map_patient(_patient_resource())
    assert isinstance(patient, Patient)
    assert patient.id == "abc-123"
    assert patient.name == "Aaron Q Alpha"
    assert patient.dob == date(1985, 3, 12)
    assert patient.sex == "male"
    assert patient.source == SourceRef(
        resource_type="Patient",
        id="abc-123",
        timestamp=datetime.fromisoformat("2026-07-09T06:08:02+00:00"),
    )


def test_map_patient_prefers_text_then_official() -> None:
    # Explicit `text` wins over structured given/family.
    r = _patient_resource(name=[{"use": "official", "text": "Dr. Alpha", "family": "X"}])
    assert map_patient(r).name == "Dr. Alpha"

    # With no `use=="official"`, the first usable entry is taken.
    r2 = _patient_resource(name=[{"family": "Beta", "given": ["Bella"]}])
    assert map_patient(r2).name == "Bella Beta"


def test_map_patient_unknown_gender_is_safe() -> None:
    assert map_patient(_patient_resource(gender="frobnicated")).sex == "unknown"
    r = _patient_resource()
    del r["gender"]
    assert map_patient(r).sex == "unknown"


def test_map_patient_handles_z_suffixed_timestamp() -> None:
    r = _patient_resource(meta={"lastUpdated": "2026-07-09T06:08:02Z"})
    ts = map_patient(r).source.timestamp
    assert ts == datetime.fromisoformat("2026-07-09T06:08:02+00:00")


def test_map_patient_missing_timestamp_still_grounds() -> None:
    r = _patient_resource(meta={})
    src = map_patient(r).source
    assert src.resource_type == "Patient" and src.id == "abc-123"
    assert src.timestamp is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.__setitem__("resourceType", "Observation"),
        lambda r: r.__setitem__("name", []),
        lambda r: r.pop("birthDate"),
        lambda r: r.__setitem__("birthDate", "not-a-date"),
        lambda r: r.pop("id"),
    ],
)
def test_map_patient_rejects_uncontractable_resource(mutate) -> None:
    r = _patient_resource()
    mutate(r)
    with pytest.raises(FhirError):
        map_patient(r)


# ---------------------------------------------------------------------------
# get_patient end-to-end (mocked OpenEMR)
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_patient_returns_grounded_tool_result(settings: Settings) -> None:
    route = respx.get(f"{FHIR_BASE}/Patient/abc-123").mock(
        return_value=httpx.Response(200, json=_patient_resource())
    )
    tokens = _StubTokens()

    async with FhirClient(tokens, settings=settings) as client:
        result = await get_patient("abc-123", client=client)

    assert isinstance(result, ToolResult)
    assert result.data.id == "abc-123"
    assert result.sources == [result.data.source]
    assert result.partial is False
    assert route.called


@respx.mock
async def test_request_carries_user_token_and_correlation_id(settings: Settings) -> None:
    route = respx.get(f"{FHIR_BASE}/Patient/abc-123").mock(
        return_value=httpx.Response(200, json=_patient_resource())
    )
    tokens = _StubTokens("the-user-token")

    token = set_correlation_id("corr-xyz")
    try:
        async with FhirClient(tokens, settings=settings) as client:
            await get_patient("abc-123", client=client)
    finally:
        reset_correlation_id(token)

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer the-user-token"
    assert request.headers[CORRELATION_ID_HEADER] == "corr-xyz"
    assert request.headers["Accept"] == "application/fhir+json"


@respx.mock
async def test_transient_5xx_is_retried_then_succeeds(settings: Settings) -> None:
    route = respx.get(f"{FHIR_BASE}/Patient/abc-123").mock(
        side_effect=[
            httpx.Response(503, json={"resourceType": "OperationOutcome"}),
            httpx.Response(200, json=_patient_resource()),
        ]
    )
    async with FhirClient(_StubTokens(), settings=settings) as client:
        result = await get_patient("abc-123", client=client)

    assert result.data.id == "abc-123"
    assert route.call_count == 2  # one retry, then success


@respx.mock
async def test_permanent_404_is_not_retried(settings: Settings) -> None:
    route = respx.get(f"{FHIR_BASE}/Patient/missing").mock(
        return_value=httpx.Response(404, json={"resourceType": "OperationOutcome"})
    )
    async with FhirClient(_StubTokens(), settings=settings) as client:
        with pytest.raises(FhirError) as exc:
            await get_patient("missing", client=client)

    assert exc.value.status_code == 404
    assert exc.value.retriable is False
    assert route.call_count == 1  # no retry on a permanent error
