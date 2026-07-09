"""Unit tests for true incremental streaming (PRP M3-2, FR-12).

Everything is mocked — a canned :class:`FakeFhirClient` and a fake LLM stand in
for OpenEMR and Anthropic, so no ``ANTHROPIC_API_KEY`` and no live stack are
touched. These exercise the progressive stream the orchestrator now emits:

* :meth:`HandRolledOrchestrator.stream_patient_summary` yields the finalized
  stages **in order** (headline → cited claims → flags → caveats → notices →
  data-as-of), rather than materialising the whole envelope first;
* a refused request streams exactly one :class:`RefusalEvent` and nothing else;
* the endpoint streams the same ordered NDJSON events over HTTP;
* an ``X-Break-Glass-Reason`` header reaches the orchestrator and flips an
  out-of-panel refusal into the granted, streamed summary.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from copilot.api.summary import get_orchestrator
from copilot.main import app
from copilot.orchestrator.cache import TTLCache
from copilot.orchestrator.controller import (
    ClaimEvent,
    DataAsOfEvent,
    HandRolledOrchestrator,
    HeadlineEvent,
    RefusalEvent,
)
from copilot.schemas.clinical import CriticalSet, Deltas
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

PATIENT = "p1"
PROVIDER = "admin"


# ---------------------------------------------------------------------------
# Fakes (no key, no network)
# ---------------------------------------------------------------------------


class FakeFhirClient:
    """A FhirClient stand-in returning canned Bundles keyed by path."""

    def __init__(self, bundles: dict[str, Any]) -> None:
        self._bundles = bundles

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        value = self._bundles.get(path)
        if value is None:
            return {"resourceType": "Bundle", "entry": []}
        return value


class FakeLLM:
    """An LLMClient stand-in returning a preset summary."""

    def __init__(self, summary: GroundedSummary) -> None:
        self._summary = summary

    async def summarize(self, critical_set: CriticalSet, deltas: Deltas) -> GroundedSummary:
        return self._summary


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}


def _appointment(patient_id: str) -> dict[str, Any]:
    return {
        "resourceType": "Appointment",
        "id": "appt-1",
        "start": "2026-07-07T09:00:00Z",
        "participant": [
            {"actor": {"reference": f"Patient/{patient_id}", "display": "Jane Roe"}}
        ],
    }


def _medication(mid: str, name: str) -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": mid,
        "status": "active",
        "authoredOn": "2026-06-01T00:00:00Z",
        "medicationCodeableConcept": {"text": name},
    }


def _in_panel_bundles(*meds: dict[str, Any]) -> dict[str, Any]:
    """A paneled patient (on today's schedule) with the given medications."""

    return {
        "/Appointment": _bundle(_appointment(PATIENT)),
        "/MedicationRequest": _bundle(*meds),
        "/AllergyIntolerance": _bundle(),
        "/Observation": _bundle(),
        "/Condition": _bundle(),
        "/Encounter": _bundle(),
    }


def _out_of_panel_bundles(*meds: dict[str, Any]) -> dict[str, Any]:
    """No schedule and no encounter — the normal gate refuses; break-glass grants."""

    return {
        "/Appointment": _bundle(),
        "/MedicationRequest": _bundle(*meds),
        "/AllergyIntolerance": _bundle(),
        "/Observation": _bundle(),
        "/Condition": _bundle(),
        "/Encounter": _bundle(),
    }


def _summary() -> GroundedSummary:
    return GroundedSummary(
        headline="Stable hypertensive on lisinopril.",
        must_knows=[
            Claim(
                text="Active on lisinopril.",
                sources=[SourceRef(resource_type="MedicationRequest", id="med-1")],
            )
        ],
        whats_changed=[],
        caveats=["Allergy list reviewed."],
    )


def _orchestrator(bundles: dict[str, Any]) -> HandRolledOrchestrator:
    return HandRolledOrchestrator(
        fhir_client=FakeFhirClient(bundles),  # type: ignore[arg-type]
        llm_client=FakeLLM(_summary()),  # type: ignore[arg-type]
        cache=TTLCache(),
    )


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Orchestrator-level: events arrive in order, progressively
# ---------------------------------------------------------------------------


async def test_stream_events_arrive_in_order() -> None:
    orch = _orchestrator(_in_panel_bundles(_medication("med-1", "Lisinopril")))

    events = [
        event async for event in orch.stream_patient_summary(PATIENT, PROVIDER)
    ]

    # Headline is emitted first (the moment the summary verifies), the closing
    # data-as-of stamp last — the ordered, finalized-stage stream.
    assert isinstance(events[0], HeadlineEvent)
    assert isinstance(events[-1], DataAsOfEvent)

    # A grounded must-know claim, cited, is streamed between them.
    claims = [e for e in events if isinstance(e, ClaimEvent)]
    assert any(c.kind == "must_know" for c in claims)
    must_know = next(c for c in claims if c.kind == "must_know")
    assert must_know.claim.sources[0].id == "med-1"


async def test_refusal_streams_a_single_event() -> None:
    orch = _orchestrator(_out_of_panel_bundles())

    events = [
        event async for event in orch.stream_patient_summary("stranger", PROVIDER)
    ]

    assert len(events) == 1
    assert isinstance(events[0], RefusalEvent)
    assert events[0].patient_id == "stranger"
    assert "refused" in events[0].reason.lower()


async def test_break_glass_reason_reaches_gate_and_streams_summary() -> None:
    orch = _orchestrator(_out_of_panel_bundles(_medication("med-1", "Lisinopril")))

    # Out of panel, but an explicit break-glass reason grants access, so the
    # summary streams instead of a lone refusal.
    events = [
        event
        async for event in orch.stream_patient_summary(
            PATIENT, PROVIDER, break_glass_reason="Covering colleague on call."
        )
    ]

    assert any(isinstance(e, HeadlineEvent) for e in events)
    assert not any(isinstance(e, RefusalEvent) for e in events)


# ---------------------------------------------------------------------------
# Endpoint-level: the same ordered stream over HTTP, plus the break-glass header
# ---------------------------------------------------------------------------


def _client(orch: HandRolledOrchestrator) -> TestClient:
    async def _override() -> HandRolledOrchestrator:
        return orch

    app.dependency_overrides[get_orchestrator] = _override
    return TestClient(app)


def test_endpoint_streams_events_in_order() -> None:
    orch = _orchestrator(_in_panel_bundles(_medication("med-1", "Lisinopril")))
    client = _client(orch)
    try:
        resp = client.post(f"/patients/{PATIENT}/summary")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    events = _events(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "headline"
    assert "must_know" in types
    assert types[-1] == "data_as_of"

    # Every rendered claim still links a source_id (grounding contract intact).
    for event in events:
        if event["type"] in {"must_know", "what_changed", "flag"}:
            assert event["sources"]
            for src in event["sources"]:
                assert src["source_id"] == f"{src['resource_type']}/{src['id']}"


def test_endpoint_break_glass_header_enables_gated_path() -> None:
    orch = _orchestrator(_out_of_panel_bundles(_medication("med-1", "Lisinopril")))
    client = _client(orch)
    try:
        # No header → out of panel → single refusal event.
        refused = client.post(f"/patients/{PATIENT}/summary")
        # With the header → break-glass override grants and streams the summary.
        granted = client.post(
            f"/patients/{PATIENT}/summary",
            headers={"X-Break-Glass-Reason": "Covering colleague on call."},
        )
    finally:
        app.dependency_overrides.clear()

    refused_events = _events(refused.text)
    assert len(refused_events) == 1
    assert refused_events[0]["type"] == "refusal"

    granted_types = [e["type"] for e in _events(granted.text)]
    assert "headline" in granted_types
    assert "refusal" not in granted_types
