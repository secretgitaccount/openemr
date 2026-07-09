"""Unit tests for the streamed patient-summary endpoint (PRP M1-7).

The orchestrator is replaced wholesale via FastAPI's dependency override, so no
OAuth, live OpenEMR stack, or ANTHROPIC_API_KEY is touched — the streaming and
citation logic is what's under test:

* a paneled patient streams a headline, cited must-know/what-changed claims (each
  linking a ``source_id``), safety flags, "couldn't retrieve X" notices (FR-12),
  and a closing "data as of" line;
* an out-of-panel patient streams exactly one refusal event and nothing else;
* every rendered claim carries at least one ``source_id`` (grounding, FR-8).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from copilot.api.summary import get_orchestrator, stream_summary
from copilot.main import app
from copilot.orchestrator.controller import Orchestrator, PatientSummary
from copilot.schemas.clinical import PanelDecision
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary
from copilot.verification.gate import VerifiedSummary
from copilot.verification.rules import RuleFlag

DATA_AS_OF = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake orchestrator + envelope factories
# ---------------------------------------------------------------------------


class FakeOrchestrator:
    """Returns a preset :class:`PatientSummary`, capturing the call args."""

    def __init__(self, result: PatientSummary) -> None:
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def patient_summary(
        self,
        patient_id: str,
        provider_id: str,
        *,
        break_glass_reason: str | None = None,
    ) -> PatientSummary:
        self.calls.append((patient_id, provider_id))
        return self._result


def _med_source() -> SourceRef:
    return SourceRef(resource_type="MedicationRequest", id="med-1")


def _granted_summary() -> PatientSummary:
    verified = VerifiedSummary(
        summary=GroundedSummary(
            headline="Stable hypertensive on lisinopril.",
            must_knows=[Claim(text="Active on lisinopril.", sources=[_med_source()])],
            whats_changed=[
                Claim(
                    text="New potassium 6.1 (high).",
                    sources=[SourceRef(resource_type="Observation", id="lab-9")],
                )
            ],
            caveats=["Allergy list reviewed."],
        ),
        dropped=[],
        flags=[
            RuleFlag(
                rule="allergy_contraindication",
                severity="high",
                message="Lisinopril vs recorded ACE-inhibitor allergy.",
                sources=[_med_source(), SourceRef(resource_type="AllergyIntolerance", id="al-1")],
            )
        ],
    )
    return PatientSummary(
        patient_id="p1",
        provider_id="admin",
        decision=PanelDecision(in_panel=True, reason="On today's schedule."),
        verified=verified,
        missing=["labs"],
        data_as_of=DATA_AS_OF,
    )


def _refused_summary() -> PatientSummary:
    return PatientSummary(
        patient_id="stranger",
        provider_id="admin",
        decision=PanelDecision(in_panel=False, reason="Not on panel; access refused."),
    )


def _client(result: PatientSummary) -> tuple[TestClient, FakeOrchestrator]:
    fake = FakeOrchestrator(result)

    async def _override() -> Orchestrator:
        return fake

    app.dependency_overrides[get_orchestrator] = _override
    return TestClient(app), fake


def _events(body: str) -> list[dict]:
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Paneled patient — streamed, cited summary
# ---------------------------------------------------------------------------


def test_paneled_patient_streams_cited_summary() -> None:
    client, fake = _client(_granted_summary())
    try:
        resp = client.post("/patients/p1/summary")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    events = _events(resp.text)
    types = [e["type"] for e in events]

    assert types[0] == "headline"
    assert "must_know" in types
    assert "what_changed" in types
    assert "flag" in types
    # FR-12 notice for the un-retrieved 'labs' tier.
    assert any(e["type"] == "notice" and e["field"] == "labs" for e in events)
    # Closing "data as of" line.
    data_as_of = [e for e in events if e["type"] == "data_as_of"]
    assert data_as_of and data_as_of[0]["timestamp"] == DATA_AS_OF.isoformat()

    # The provider was resolved from auth context (dev default = admin).
    assert fake.calls == [("p1", "admin")]


def test_every_rendered_claim_links_a_source_id() -> None:
    client, _ = _client(_granted_summary())
    try:
        resp = client.post("/patients/p1/summary")
    finally:
        app.dependency_overrides.clear()

    events = _events(resp.text)
    for event in events:
        if event["type"] in {"must_know", "what_changed", "flag"}:
            assert event["sources"], f"{event['type']} must carry sources"
            for src in event["sources"]:
                assert src["source_id"] == f"{src['resource_type']}/{src['id']}"


def test_notice_names_the_field_not_none() -> None:
    """A missing tier reports the field name, never a bare 'none' (FR-12)."""

    client, _ = _client(_granted_summary())
    try:
        resp = client.post("/patients/p1/summary")
    finally:
        app.dependency_overrides.clear()

    notice = next(e for e in _events(resp.text) if e["type"] == "notice")
    assert notice["field"] == "labs"
    assert "labs" in notice["text"]
    assert notice["text"].lower() != "none"


# ---------------------------------------------------------------------------
# Out-of-panel patient — single refusal event
# ---------------------------------------------------------------------------


def test_out_of_panel_patient_streams_single_refusal() -> None:
    client, _ = _client(_refused_summary())
    try:
        resp = client.post("/patients/stranger/summary")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    events = _events(resp.text)
    assert len(events) == 1
    assert events[0]["type"] == "refusal"
    assert events[0]["patient_id"] == "stranger"
    assert "refused" in events[0]["reason"].lower()


def test_provider_header_overrides_dev_default() -> None:
    client, fake = _client(_granted_summary())
    try:
        client.post("/patients/p1/summary", headers={"X-Provider-Id": "dr-house"})
    finally:
        app.dependency_overrides.clear()

    assert fake.calls == [("p1", "dr-house")]


# ---------------------------------------------------------------------------
# stream_summary unit behaviour (no HTTP)
# ---------------------------------------------------------------------------


def test_stream_summary_refusal_is_terminal() -> None:
    lines = list(stream_summary(_refused_summary()))
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "refusal"
