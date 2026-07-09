"""Integration tests for the wired M2 surface (PRP M2-5).

Everything is mocked — the Anthropic SDK is replaced by a fake LLM and OpenEMR by
a canned :class:`FakeFhirClient`, so no ``ANTHROPIC_API_KEY`` and no live stack
are needed. These exercise the *composed* M2 lifecycle behind the endpoints:

* a role-denied identity is refused **before any clinical read** (M2-3 role gate
  short-circuits ahead of the panel gate and retrieval);
* a follow-up over a started conversation resolves the **pinned** patient and
  returns cited, grounded claims (M2-2 conversation + M1-6 grounding);
* a planted drug-drug interaction surfaces as a deterministic ``flag`` through the
  wired summary path (M2-1 rule engine via the verification gate);
* ``POST /prewarm`` returns a :class:`PrewarmReport` (M2-4 data-only prewarm).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from copilot.orchestrator import controller as controller_mod
from copilot.orchestrator import prewarm as prewarm_mod
from copilot.orchestrator.cache import TTLCache
from copilot.orchestrator.controller import HandRolledOrchestrator
from copilot.openemr.roles import Role
from copilot.schemas.clinical import CriticalSet, Deltas
from copilot.schemas.conversation import GroundedAnswer
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

PATIENT = "p1"
PROVIDER = "admin"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFhirClient:
    """A FhirClient stand-in returning canned Bundles keyed by path.

    Records every touched path so a test can prove no clinical resource was read
    when a request is refused at a gate.
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


class StubTokenSource:
    """A trivial token source for the role gate (its token is never inspected)."""

    def get_access_token(self) -> str:
        return "stub-token"


class FakeLLM:
    """An LLMClient stand-in with both the summary and follow-up coroutines."""

    def __init__(
        self,
        *,
        summary: GroundedSummary | None = None,
        answer: GroundedAnswer | None = None,
    ) -> None:
        self._summary = summary
        self._answer = answer
        self.summarize_calls: list[tuple[CriticalSet, Deltas]] = []
        self.followup_calls: list[tuple[str, list[Any], CriticalSet, Deltas]] = []

    async def summarize(self, critical_set: CriticalSet, deltas: Deltas) -> GroundedSummary:
        self.summarize_calls.append((critical_set, deltas))
        assert self._summary is not None
        return self._summary

    async def answer_followup(
        self,
        question: str,
        history: list[Any],
        critical_set: CriticalSet,
        deltas: Deltas,
    ) -> GroundedAnswer:
        self.followup_calls.append((question, history, critical_set, deltas))
        assert self._answer is not None
        return self._answer


# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------


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


def _in_panel_bundles(*meds: dict[str, Any]) -> dict[str, Any]:
    """A paneled patient with a small chart carrying the given medications."""

    return {
        "/Appointment": _bundle(_appointment(PATIENT)),
        "/MedicationRequest": _bundle(*meds),
        "/AllergyIntolerance": _bundle(),
        "/Observation": _bundle(),
        "/Condition": _bundle(_condition()),
        "/Encounter": _bundle(
            _encounter("enc-2", "2026-07-01T09:00:00Z"),
            _encounter("enc-1", "2026-05-01T09:00:00Z"),
        ),
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


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. Role gate: a non-clinical identity is refused BEFORE any clinical read
# ---------------------------------------------------------------------------


async def test_role_denied_identity_refused_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_resolve(*, client: Any, **_: Any) -> Role:
        return Role.FRONT_OFFICE

    monkeypatch.setattr(controller_mod, "resolve_role", _fake_resolve)

    fhir = FakeFhirClient(_in_panel_bundles(_medication("med-1", "Lisinopril")))
    llm = FakeLLM(summary=_summary())
    orch = HandRolledOrchestrator(
        fhir_client=fhir,  # type: ignore[arg-type]
        llm_client=llm,  # type: ignore[arg-type]
        token_source=StubTokenSource(),
    )

    result = await orch.patient_summary(PATIENT, PROVIDER)

    # Refused at the role gate: no verified summary, no LLM call.
    assert result.refused is True
    assert result.verified is None
    assert llm.summarize_calls == []

    # Not a single resource — clinical *or* panel — was fetched: the role gate
    # short-circuits ahead of everything.
    assert fhir.touched_paths == set()


# ---------------------------------------------------------------------------
# 2. Follow-up resolves the pinned patient and returns cited claims
# ---------------------------------------------------------------------------


def _followup_answer() -> GroundedAnswer:
    return GroundedAnswer(
        answer=[
            Claim(
                text="She is on lisinopril for hypertension.",
                sources=[SourceRef(resource_type="MedicationRequest", id="med-1")],
            ),
            Claim(
                text="On a phantom drug not in the chart.",
                sources=[SourceRef(resource_type="MedicationRequest", id="ghost")],
            ),
        ],
        caveats=["No allergy data reviewed."],
    )


def _chat_client(orch: HandRolledOrchestrator) -> TestClient:
    from copilot.api.chat import get_chat_orchestrator
    from copilot.main import app

    async def _override() -> HandRolledOrchestrator:
        return orch

    app.dependency_overrides[get_chat_orchestrator] = _override
    return TestClient(app)


def test_followup_resolves_pinned_patient_and_cites() -> None:
    fhir = FakeFhirClient(_in_panel_bundles(_medication("med-1", "Lisinopril")))
    llm = FakeLLM(summary=_summary(), answer=_followup_answer())
    # A shared cache + one orchestrator instance so the follow-up resolves the
    # conversation the start request pinned (no token source → role gate skipped).
    orch = HandRolledOrchestrator(
        fhir_client=fhir,  # type: ignore[arg-type]
        llm_client=llm,  # type: ignore[arg-type]
        cache=TTLCache(),
    )
    client = _chat_client(orch)
    try:
        start = client.post(f"/patients/{PATIENT}/conversation")
        assert start.status_code == 200
        start_events = _events(start.text)
        convo = next(e for e in start_events if e["type"] == "conversation")
        conversation_id = convo["conversation_id"]
        assert convo["patient_id"] == PATIENT

        # The follow-up never names the patient — it must resolve from the pin.
        resp = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"question": "What is she taking?"},
        )
    finally:
        from copilot.main import app

        app.dependency_overrides.clear()

    assert resp.status_code == 200
    events = _events(resp.text)

    # The pinned patient was resolved into the answer envelope.
    convo_evt = next(e for e in events if e["type"] == "conversation")
    assert convo_evt["patient_id"] == PATIENT

    # The grounded claim is cited; the phantom claim is dropped by grounding.
    answers = [e for e in events if e["type"] == "answer"]
    assert len(answers) == 1
    assert answers[0]["sources"][0]["source_id"] == "MedicationRequest/med-1"
    assert any(e["type"] == "dropped" for e in events)

    # The follow-up ran against the pinned patient's retained critical set.
    assert llm.followup_calls, "expected the follow-up LLM step to run"
    _, _, retained, _ = llm.followup_calls[0]
    assert retained.medications[0].id == "med-1"


def test_unknown_conversation_returns_404() -> None:
    fhir = FakeFhirClient(_in_panel_bundles(_medication("med-1", "Lisinopril")))
    llm = FakeLLM(summary=_summary(), answer=_followup_answer())
    orch = HandRolledOrchestrator(
        fhir_client=fhir,  # type: ignore[arg-type]
        llm_client=llm,  # type: ignore[arg-type]
        cache=TTLCache(),
    )
    client = _chat_client(orch)
    try:
        resp = client.post(
            "/conversations/does-not-exist/messages",
            json={"question": "anything?"},
        )
    finally:
        from copilot.main import app

        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. A planted interaction surfaces as a deterministic flag (summary path)
# ---------------------------------------------------------------------------


def test_planted_interaction_flags_through_summary_path() -> None:
    # Two interacting active meds: warfarin + an NSAID (ibuprofen) → high flag,
    # deterministically, even though the fake summary never mentions it.
    fhir = FakeFhirClient(
        _in_panel_bundles(
            _medication("med-1", "Warfarin"),
            _medication("med-2", "Ibuprofen"),
        )
    )
    llm = FakeLLM(summary=_summary(), answer=_followup_answer())
    orch = HandRolledOrchestrator(
        fhir_client=fhir,  # type: ignore[arg-type]
        llm_client=llm,  # type: ignore[arg-type]
        cache=TTLCache(),
    )
    client = _chat_client(orch)
    try:
        resp = client.post(f"/patients/{PATIENT}/conversation")
    finally:
        from copilot.main import app

        app.dependency_overrides.clear()

    assert resp.status_code == 200
    flags = [e for e in _events(resp.text) if e["type"] == "flag"]
    assert any(f["rule"] == "drug_interaction" for f in flags)
    interaction = next(f for f in flags if f["rule"] == "drug_interaction")
    assert interaction["severity"] == "high"


# ---------------------------------------------------------------------------
# 4. /prewarm returns a report
# ---------------------------------------------------------------------------


def test_prewarm_endpoint_returns_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from copilot.schemas.clinical import Medication, ScheduledPatient
    from copilot.schemas.core import ToolResult

    patient_ids = ["pat-1", "pat-2"]

    async def _fake_schedule(provider_id: str, *, client: Any) -> ToolResult:
        return ToolResult(
            data=[
                ScheduledPatient(
                    patient_id=pid,
                    name=f"Patient {pid}",
                    start=__import__("datetime").datetime(
                        2026, 7, 7, 9, 0, tzinfo=__import__("datetime").timezone.utc
                    ),
                    appointment_id=f"appt-{pid}",
                    source=SourceRef(resource_type="Appointment", id=f"appt-{pid}"),
                )
                for pid in patient_ids
            ]
        )

    async def _fake_get(patient_id: str, *, client: Any) -> CriticalSet:
        return CriticalSet(
            medications=[
                Medication(
                    id=f"med-{patient_id}",
                    name="Lisinopril",
                    status="active",
                    source=SourceRef(resource_type="MedicationRequest", id=f"med-{patient_id}"),
                )
            ]
        )

    monkeypatch.setattr(prewarm_mod, "get_todays_schedule", _fake_schedule)
    monkeypatch.setattr(prewarm_mod, "get_critical_set", _fake_get)

    from copilot.api.prewarm import get_prewarm_client
    from copilot.main import app

    async def _override_client() -> Any:
        return object()  # the stubbed retrievals ignore the client

    app.dependency_overrides[get_prewarm_client] = _override_client
    try:
        resp = TestClient(app).post("/prewarm")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    report = resp.json()
    assert report["scheduled"] == 2
    assert report["warmed"] == 2
    assert report["failed"] == []
