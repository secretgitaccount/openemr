"""Stub-LLM mode unit tests (PRP M3-4).

The load tests in ``loadtest/`` run the agent with ``COPILOT_LLM_STUB=1`` so
throughput is measured without any Anthropic spend. These tests prove that in
stub mode :class:`LLMClient` returns a canned, source-bound
``GroundedSummary``/``GroundedAnswer`` and **never** touches the Anthropic HTTP
API (``respx`` asserts no request to ``api.anthropic.com`` is made, and no key
is configured).
"""

from __future__ import annotations

import respx

from copilot.config import Settings
from copilot.llm.client import LLMClient
from copilot.schemas.clinical import (
    Allergy,
    CriticalSet,
    Deltas,
    LabResult,
    Medication,
    Problem,
)
from copilot.schemas.conversation import ConversationTurn, GroundedAnswer
from copilot.schemas.core import SourceRef
from copilot.schemas.output import GroundedSummary

ANTHROPIC_HOST = "https://api.anthropic.com"


def _stub_settings() -> Settings:
    # No real key at all: stub mode must not need one.
    return Settings(anthropic_api_key="sk-ant-xxxxxxxx", copilot_llm_stub=True)


def _critical_set() -> CriticalSet:
    return CriticalSet(
        medications=[
            Medication(
                id="med-1",
                name="Lisinopril",
                status="active",
                dosage="10 mg daily",
                source=SourceRef(resource_type="MedicationRequest", id="med-1"),
            )
        ],
        allergies=[
            Allergy(
                id="alg-1",
                substance="Penicillin",
                reaction="hives",
                criticality="high",
                source=SourceRef(resource_type="AllergyIntolerance", id="alg-1"),
            )
        ],
        labs=[
            LabResult(
                id="lab-1",
                name="Potassium",
                value="6.1",
                unit="mmol/L",
                abnormal=True,
                source=SourceRef(resource_type="Observation", id="lab-1"),
            )
        ],
        problems=[
            Problem(
                id="prob-1",
                name="Hypertension",
                clinical_status="active",
                source=SourceRef(resource_type="Condition", id="prob-1"),
            )
        ],
        missing=[],
    )


def _deltas() -> Deltas:
    return Deltas(
        new_meds=[
            Medication(
                id="med-2",
                name="Metformin",
                status="active",
                dosage="500 mg BID",
                source=SourceRef(resource_type="MedicationRequest", id="med-2"),
            )
        ],
    )


@respx.mock
async def test_stub_summarize_returns_grounded_without_anthropic() -> None:
    """Stub mode yields a source-bound summary and makes no Anthropic call."""

    route = respx.route(host="api.anthropic.com")

    client = LLMClient(settings=_stub_settings())
    summary = await client.summarize(_critical_set(), _deltas())

    assert isinstance(summary, GroundedSummary)
    assert summary.headline
    # Canned claims cite real input records so they survive verification.
    assert summary.must_knows and all(c.is_grounded for c in summary.must_knows)
    assert summary.whats_changed and all(c.is_grounded for c in summary.whats_changed)
    med_ids = {s.id for c in summary.must_knows for s in c.sources}
    assert "med-1" in med_ids
    changed_ids = {s.id for c in summary.whats_changed for s in c.sources}
    assert "med-2" in changed_ids

    assert not route.called, "stub mode must not call Anthropic"


@respx.mock
async def test_stub_answer_returns_grounded_without_anthropic() -> None:
    """Stub mode answers a follow-up, grounded, with no Anthropic call."""

    route = respx.route(host="api.anthropic.com")

    client = LLMClient(settings=_stub_settings())
    history = [ConversationTurn(role="user", text="what are her allergies?")]
    answer = await client.answer_followup(
        "what are her allergies?", history, _critical_set(), _deltas()
    )

    assert isinstance(answer, GroundedAnswer)
    assert answer.answer and all(c.is_grounded for c in answer.answer)
    assert not route.called, "stub mode must not call Anthropic"


async def test_stub_summary_grounds_only_available_tiers() -> None:
    """With an empty critical set the stub yields a valid, claim-free summary."""

    client = LLMClient(settings=_stub_settings())
    summary = await client.summarize(CriticalSet(missing=["all"]), Deltas())

    assert isinstance(summary, GroundedSummary)
    assert summary.must_knows == []
    assert summary.whats_changed == []
    assert summary.caveats
