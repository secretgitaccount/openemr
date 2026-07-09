"""Unit tests for the conversation endpoints (PRP M3-2).

Everything is mocked — a canned :class:`FakeFhirClient` and a fake LLM stand in
for OpenEMR and Anthropic, so no ``ANTHROPIC_API_KEY`` and no live stack are
touched. These cover the two M3-2 endpoint refinements on the chat surface:

* an ``X-Break-Glass-Reason`` header threads through conversation start, so an
  out-of-panel patient is reachable over HTTP (a lone refusal without it);
* a "what changed since last visit" follow-up **grounds against recomputed
  deltas** — a claim citing a delta-only record (a new encounter, absent from
  the plain retained critical set) is kept, not dropped (the M2-5 gap closed).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from copilot.api.chat import get_chat_orchestrator
from copilot.main import app
from copilot.orchestrator.cache import TTLCache
from copilot.orchestrator.controller import HandRolledOrchestrator
from copilot.schemas.clinical import CriticalSet, Deltas
from copilot.schemas.conversation import GroundedAnswer
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
    """An LLMClient stand-in with both the summary and follow-up coroutines."""

    def __init__(self, *, summary: GroundedSummary, answer: GroundedAnswer) -> None:
        self._summary = summary
        self._answer = answer
        self.followup_calls: list[tuple[str, list[Any], CriticalSet, Deltas]] = []

    async def summarize(self, critical_set: CriticalSet, deltas: Deltas) -> GroundedSummary:
        return self._summary

    async def answer_followup(
        self,
        question: str,
        history: list[Any],
        critical_set: CriticalSet,
        deltas: Deltas,
    ) -> GroundedAnswer:
        self.followup_calls.append((question, history, critical_set, deltas))
        return self._answer


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


def _encounter(eid: str, start: str) -> dict[str, Any]:
    return {
        "resourceType": "Encounter",
        "id": eid,
        "class": {"code": "AMB"},
        "period": {"start": start},
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
        caveats=[],
    )


def _chat_client(orch: HandRolledOrchestrator) -> TestClient:
    async def _override() -> HandRolledOrchestrator:
        return orch

    app.dependency_overrides[get_chat_orchestrator] = _override
    return TestClient(app)


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. Break-glass header threads through conversation start
# ---------------------------------------------------------------------------


def _out_of_panel_bundles() -> dict[str, Any]:
    """No schedule, no encounter — the normal gate refuses; break-glass grants."""

    return {
        "/Appointment": _bundle(),
        "/MedicationRequest": _bundle(_medication("med-1", "Lisinopril")),
        "/AllergyIntolerance": _bundle(),
        "/Observation": _bundle(),
        "/Condition": _bundle(),
        "/Encounter": _bundle(),
    }


def _orchestrator(bundles: dict[str, Any], answer: GroundedAnswer) -> HandRolledOrchestrator:
    return HandRolledOrchestrator(
        fhir_client=FakeFhirClient(bundles),  # type: ignore[arg-type]
        llm_client=FakeLLM(summary=_summary(), answer=answer),  # type: ignore[arg-type]
        cache=TTLCache(),
    )


def test_break_glass_header_starts_conversation_over_override() -> None:
    answer = GroundedAnswer(answer=[], caveats=[])
    orch = _orchestrator(_out_of_panel_bundles(), answer)
    client = _chat_client(orch)
    try:
        refused = client.post(f"/patients/{PATIENT}/conversation")
        granted = client.post(
            f"/patients/{PATIENT}/conversation",
            headers={"X-Break-Glass-Reason": "Covering colleague on call."},
        )
    finally:
        app.dependency_overrides.clear()

    # No header → out of panel → single refusal, nothing pinned.
    refused_events = _events(refused.text)
    assert len(refused_events) == 1
    assert refused_events[0]["type"] == "refusal"
    assert not any(e["type"] == "conversation" for e in refused_events)

    # With the header → break-glass grants and a conversation is pinned.
    granted_events = _events(granted.text)
    assert any(e["type"] == "headline" for e in granted_events)
    convo = next(e for e in granted_events if e["type"] == "conversation")
    assert convo["patient_id"] == PATIENT
    assert convo["conversation_id"]


# ---------------------------------------------------------------------------
# 2. A "what changed" follow-up grounds against recomputed deltas
# ---------------------------------------------------------------------------


def _in_panel_with_history() -> dict[str, Any]:
    """A paneled patient with two dated encounters, so deltas has a new encounter.

    ``enc-2`` (2026-07-01) is newer than the reference visit ``enc-1``
    (2026-05-01), so ``get_deltas_since_last_visit`` places ``enc-2`` in
    ``new_encounters`` — a record that lives *only* in the deltas, never in the
    plain critical set.
    """

    return {
        "/Appointment": _bundle(_appointment(PATIENT)),
        "/MedicationRequest": _bundle(_medication("med-1", "Lisinopril")),
        "/AllergyIntolerance": _bundle(),
        "/Observation": _bundle(),
        "/Condition": _bundle(),
        "/Encounter": _bundle(
            _encounter("enc-2", "2026-07-01T09:00:00Z"),
            _encounter("enc-1", "2026-05-01T09:00:00Z"),
        ),
    }


def _what_changed_answer() -> GroundedAnswer:
    return GroundedAnswer(
        answer=[
            # Cites a delta-only record (the new encounter) — grounds only if the
            # deltas are recomputed and attached to the retained set (M3-2).
            Claim(
                text="A new visit occurred on 2026-07-01.",
                sources=[SourceRef(resource_type="Encounter", id="enc-2")],
            ),
            # Cites a record present nowhere — must still be dropped by grounding.
            Claim(
                text="On a phantom drug not in the chart.",
                sources=[SourceRef(resource_type="MedicationRequest", id="ghost")],
            ),
        ],
        caveats=[],
    )


def test_what_changed_followup_grounds_against_recomputed_deltas() -> None:
    orch = _orchestrator(_in_panel_with_history(), _what_changed_answer())
    client = _chat_client(orch)
    try:
        start = client.post(f"/patients/{PATIENT}/conversation")
        assert start.status_code == 200
        conversation_id = next(
            e for e in _events(start.text) if e["type"] == "conversation"
        )["conversation_id"]

        resp = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"question": "What changed since her last visit?"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    events = _events(resp.text)

    # The delta-sourced claim is KEPT (grounded against the recomputed deltas),
    # citing the new encounter — the M2-5 "what changed" gap is closed.
    answers = [e for e in events if e["type"] == "answer"]
    assert len(answers) == 1
    assert answers[0]["sources"][0]["source_id"] == "Encounter/enc-2"

    # The phantom claim is still dropped by grounding — the gate stays strict.
    dropped = [e for e in events if e["type"] == "dropped"]
    assert any("phantom" in d["text"].lower() for d in dropped)
