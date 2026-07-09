"""Unit tests for the patient-panel gate + agent-side audit (PRP M1-2).

OpenEMR's FHIR API is mocked with ``respx`` (the async httpx transport is
intercepted); the live end-to-end proof is the
``python -m copilot.openemr.panel --provider admin --patient <id>`` gate in the
PRP's Validation.

Coverage:

* today's schedule maps ``Appointment`` resources to grounded
  :class:`ScheduledPatient` entries (and skips uncontractable ones);
* in-panel via the schedule → grounded on the ``Appointment``;
* in-panel via the recent-encounter fallback → grounded on the ``Encounter``;
* out-of-panel → ``in_panel=False``, no source, and a logged
  ``copilot.audit.refusal`` carrying the ids + correlation ID;
* break-glass → ``in_panel=True, break_glass=True`` + a logged
  ``copilot.audit.break_glass``; an empty reason is refused.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest
import respx

from copilot.config import Settings
from copilot.logging import (
    configure_logging,
    reset_correlation_id,
    set_correlation_id,
)
from copilot.openemr.client import FhirClient
from copilot.openemr.panel import (
    break_glass,
    get_todays_schedule,
    is_patient_in_panel,
)
from copilot.schemas.clinical import PanelDecision, ScheduledPatient
from copilot.schemas.core import ToolResult

FHIR_BASE = "http://oemr.test/apis/default/fhir"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openemr_base_url="http://oemr.test",
        openemr_fhir_base=FHIR_BASE,
        openemr_oauth_base="http://oemr.test/oauth2/default",
    )


@pytest.fixture
def log_stream() -> io.StringIO:
    """Redirect structlog JSON output into an in-memory buffer for assertions."""

    buffer = io.StringIO()
    configure_logging(level="INFO", stream=buffer)
    yield buffer
    configure_logging()


def _log_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class _StubTokens:
    """Minimal ``TokenSource``: returns a fixed access token."""

    def get_access_token(self) -> str:
        return "user-access-token"


def _appointment(
    appointment_id: str = "appt-1",
    patient_id: str = "pat-1",
    *,
    name: str | None = "John Doe",
    start: str | None = "2026-07-07T09:00:00Z",
) -> dict:
    actor: dict = {"reference": f"Patient/{patient_id}"}
    if name is not None:
        actor["display"] = name
    resource: dict = {
        "resourceType": "Appointment",
        "id": appointment_id,
        "participant": [
            {"actor": actor},
            {"actor": {"reference": "Practitioner/admin", "display": "Dr Admin"}},
        ],
    }
    if start is not None:
        resource["start"] = start
    return resource


def _bundle(*resources: dict) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }


def _encounter(encounter_id: str = "enc-1", start: str = "2026-06-01T10:00:00Z") -> dict:
    return {
        "resourceType": "Encounter",
        "id": encounter_id,
        "period": {"start": start},
    }


def _fhir_client(settings: Settings) -> FhirClient:
    return FhirClient(_StubTokens(), settings=settings)


# ---------------------------------------------------------------------------
# get_todays_schedule
# ---------------------------------------------------------------------------


@respx.mock
async def test_todays_schedule_maps_appointments(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/Appointment").mock(
        return_value=httpx.Response(
            200,
            json=_bundle(
                _appointment("appt-1", "pat-1", name="John Doe"),
                _appointment("appt-2", "pat-2", name=None),
            ),
        )
    )

    async with _fhir_client(settings) as client:
        result = await get_todays_schedule("admin", client=client)

    assert isinstance(result, ToolResult)
    assert [p.patient_id for p in result.data] == ["pat-1", "pat-2"]
    first: ScheduledPatient = result.data[0]
    assert first.name == "John Doe"
    assert first.appointment_id == "appt-1"
    assert first.source.resource_type == "Appointment"
    # A participant with no display still grounds (name falls back to the id).
    assert result.data[1].name == "Patient pat-2"
    # Sources mirror the scheduled patients.
    assert result.sources == [p.source for p in result.data]


@respx.mock
async def test_todays_schedule_skips_uncontractable_appointments(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/Appointment").mock(
        return_value=httpx.Response(
            200,
            json=_bundle(
                _appointment("appt-1", "pat-1"),
                _appointment("appt-2", "pat-2", start=None),  # no start → skipped
            ),
        )
    )

    async with _fhir_client(settings) as client:
        result = await get_todays_schedule("admin", client=client)

    assert [p.patient_id for p in result.data] == ["pat-1"]


@respx.mock
async def test_todays_schedule_degrades_when_index_unavailable(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/Appointment").mock(return_value=httpx.Response(500))

    async with _fhir_client(settings) as client:
        result = await get_todays_schedule("admin", client=client)

    assert result.data == []
    assert result.partial is True
    assert result.missing == ["schedule"]


# ---------------------------------------------------------------------------
# is_patient_in_panel
# ---------------------------------------------------------------------------


@respx.mock
async def test_in_panel_via_schedule(settings: Settings) -> None:
    respx.get(f"{FHIR_BASE}/Appointment").mock(
        return_value=httpx.Response(200, json=_bundle(_appointment("appt-1", "pat-1")))
    )

    async with _fhir_client(settings) as client:
        result = await is_patient_in_panel("pat-1", "admin", client=client)

    decision: PanelDecision = result.data
    assert decision.in_panel is True
    assert decision.break_glass is False
    assert decision.source is not None
    assert decision.source.resource_type == "Appointment"
    assert decision.source.id == "appt-1"
    assert result.sources == [decision.source]


@respx.mock
async def test_in_panel_via_recent_encounter_fallback(settings: Settings) -> None:
    # Not on the schedule, but has a recent encounter with the provider.
    respx.get(f"{FHIR_BASE}/Appointment").mock(
        return_value=httpx.Response(200, json=_bundle())
    )
    respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(200, json=_bundle(_encounter("enc-9")))
    )

    async with _fhir_client(settings) as client:
        result = await is_patient_in_panel("pat-1", "admin", client=client)

    decision = result.data
    assert decision.in_panel is True
    assert decision.source is not None
    assert decision.source.resource_type == "Encounter"
    assert decision.source.id == "enc-9"


@respx.mock
async def test_out_of_panel_is_refused_and_logged(
    settings: Settings, log_stream: io.StringIO
) -> None:
    respx.get(f"{FHIR_BASE}/Appointment").mock(
        return_value=httpx.Response(200, json=_bundle())
    )
    respx.get(f"{FHIR_BASE}/Encounter").mock(
        return_value=httpx.Response(200, json=_bundle())
    )

    token = set_correlation_id("corr-refuse")
    try:
        async with _fhir_client(settings) as client:
            result = await is_patient_in_panel("outsider", "admin", client=client)
    finally:
        reset_correlation_id(token)

    decision = result.data
    assert decision.in_panel is False
    assert decision.break_glass is False
    assert decision.source is None
    assert result.sources == []

    refusals = [ln for ln in _log_lines(log_stream) if ln["event"] == "copilot.audit.refusal"]
    assert refusals, "expected a copilot.audit.refusal audit event"
    entry = refusals[-1]
    assert entry["patient_id"] == "outsider"
    assert entry["provider_id"] == "admin"
    assert entry["correlation_id"] == "corr-refuse"
    assert entry["reason"]  # non-empty human-readable reason


# ---------------------------------------------------------------------------
# break_glass
# ---------------------------------------------------------------------------


@respx.mock
async def test_break_glass_grants_and_audits(
    settings: Settings, log_stream: io.StringIO
) -> None:
    token = set_correlation_id("corr-break")
    try:
        async with _fhir_client(settings) as client:
            result = await break_glass(
                "outsider", "admin", "covering colleague on call", client=client
            )
    finally:
        reset_correlation_id(token)

    decision = result.data
    assert decision.in_panel is True
    assert decision.break_glass is True
    assert "covering colleague on call" in decision.reason

    events = [
        ln for ln in _log_lines(log_stream) if ln["event"] == "copilot.audit.break_glass"
    ]
    assert events, "expected a copilot.audit.break_glass audit event"
    entry = events[-1]
    assert entry["patient_id"] == "outsider"
    assert entry["provider_id"] == "admin"
    assert entry["correlation_id"] == "corr-break"
    assert entry["reason"] == "covering colleague on call"


@respx.mock
async def test_break_glass_refuses_empty_reason(
    settings: Settings, log_stream: io.StringIO
) -> None:
    async with _fhir_client(settings) as client:
        with pytest.raises(ValueError):
            await break_glass("outsider", "admin", "   ", client=client)

    # A refused override must not emit a break-glass audit event.
    events = [
        ln for ln in _log_lines(log_stream) if ln["event"] == "copilot.audit.break_glass"
    ]
    assert events == []
