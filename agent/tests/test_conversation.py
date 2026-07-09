"""Unit tests for short-lived cache + multi-turn conversation (PRP M2-2).

Covers:

* :class:`TTLCache` get/set round-trip and monotonic TTL expiry (via an injected
  fake clock — no sleeping);
* :meth:`ConversationStore.start` pins a patient;
* two ``append`` + ``get`` calls preserve history across *separate* store
  instances sharing one cache (simulating separate requests / replicas);
* an expired conversation is dropped (``ConversationNotFoundError``);
* a follow-up cannot change the pinned ``patient_id``;
* :meth:`LLMClient.answer_followup` (Anthropic mocked with ``respx``) is called
  with the pinned patient's records + prior turns and parses a
  :class:`GroundedAnswer`.

No ANTHROPIC_API_KEY and no network are needed.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic

from copilot.config import Settings
from copilot.llm.client import LLMClient, build_followup_payload
from copilot.orchestrator.cache import TTLCache
from copilot.orchestrator.conversation import (
    ConversationNotFoundError,
    ConversationState,
    ConversationStore,
)
from copilot.schemas.clinical import Allergy, CriticalSet, Deltas, Medication
from copilot.schemas.conversation import ConversationTurn, GroundedAnswer
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim

MESSAGES_URL = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """A manually-advanced monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="sk-ant-real", anthropic_model="claude-sonnet-5")


def _anthropic() -> AsyncAnthropic:
    return AsyncAnthropic(api_key="test-key", max_retries=0)


def _critical_set() -> CriticalSet:
    return CriticalSet(
        medications=[
            Medication(
                id="med-1",
                name="Lisinopril",
                status="active",
                source=SourceRef(resource_type="MedicationRequest", id="med-1"),
            )
        ],
        allergies=[
            Allergy(
                id="alg-1",
                substance="Penicillin",
                criticality="high",
                source=SourceRef(resource_type="AllergyIntolerance", id="alg-1"),
            )
        ],
    )


def _deltas() -> Deltas:
    return Deltas()


def _answer() -> GroundedAnswer:
    return GroundedAnswer(
        answer=[
            Claim(
                text="Penicillin allergy, high criticality.",
                sources=[SourceRef(resource_type="AllergyIntolerance", id="alg-1")],
            )
        ],
        caveats=[],
    )


def _answer_body(answer: GroundedAnswer, *, stop_reason: str = "end_turn") -> dict:
    content = []
    if answer is not None:
        content.append({"type": "text", "text": answer.model_dump_json()})
    return {
        "id": "msg_f1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 90, "output_tokens": 30},
    }


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------


async def test_ttl_cache_round_trip() -> None:
    cache: TTLCache[str] = TTLCache()
    assert await cache.get("k") is None
    await cache.set("k", "v", ttl=60)
    assert await cache.get("k") == "v"


async def test_ttl_cache_expires_on_monotonic_clock() -> None:
    clock = _FakeClock()
    cache: TTLCache[str] = TTLCache(clock=clock)
    await cache.set("k", "v", ttl=10)

    clock.advance(9)
    assert await cache.get("k") == "v"

    clock.advance(1)  # now == expiry -> expired
    assert await cache.get("k") is None


# ---------------------------------------------------------------------------
# ConversationStore: start pins a patient
# ---------------------------------------------------------------------------


async def test_start_pins_patient() -> None:
    store = ConversationStore(TTLCache())
    cid = await store.start("patient-7", critical_set_ref="cs-ref-7")

    state = await store.get(cid)
    assert isinstance(state, ConversationState)
    assert state.patient_id == "patient-7"
    assert state.critical_set_ref == "cs-ref-7"
    assert state.turns == []


# ---------------------------------------------------------------------------
# History preserved across separate requests (shared cache, new store each time)
# ---------------------------------------------------------------------------


async def test_history_preserved_across_separate_store_instances() -> None:
    cache: TTLCache[ConversationState] = TTLCache()

    # Request 1: start (one replica).
    cid = await ConversationStore(cache).start("patient-1", critical_set_ref="cs-1")

    # Request 2: append a user turn (a fresh store, as on another replica).
    await ConversationStore(cache).append(
        cid, ConversationTurn(role="user", text="What are her allergies?")
    )

    # Request 3: append an assistant turn (another fresh store).
    await ConversationStore(cache).append(
        cid, ConversationTurn(role="assistant", text="Penicillin (high).")
    )

    # Request 4: read back — full history survived across all of them.
    state = await ConversationStore(cache).get(cid)
    assert [t.role for t in state.turns] == ["user", "assistant"]
    assert state.turns[0].text == "What are her allergies?"
    assert state.turns[1].text == "Penicillin (high)."
    assert state.patient_id == "patient-1"


# ---------------------------------------------------------------------------
# TTL expiry drops conversation state
# ---------------------------------------------------------------------------


async def test_ttl_expiry_drops_conversation_state() -> None:
    clock = _FakeClock()
    cache: TTLCache[ConversationState] = TTLCache(clock=clock)
    store = ConversationStore(cache, ttl=30)

    cid = await store.start("patient-2", critical_set_ref="cs-2")
    assert (await store.get(cid)).patient_id == "patient-2"

    clock.advance(30)  # TTL elapsed
    with pytest.raises(ConversationNotFoundError):
        await store.get(cid)


async def test_get_unknown_conversation_raises() -> None:
    store = ConversationStore(TTLCache())
    with pytest.raises(ConversationNotFoundError):
        await store.get("nope")

    with pytest.raises(ConversationNotFoundError):
        await store.append("nope", ConversationTurn(role="user", text="hi"))


# ---------------------------------------------------------------------------
# A follow-up cannot change the pinned patient_id
# ---------------------------------------------------------------------------


async def test_append_cannot_change_pinned_patient() -> None:
    store = ConversationStore(TTLCache())
    cid = await store.start("patient-A", critical_set_ref="cs-A")

    # Even a turn that names another patient does not (and structurally cannot)
    # re-pin the conversation — append has no patient parameter.
    updated = await store.append(
        cid, ConversationTurn(role="user", text="Now show me patient-B's labs")
    )
    assert updated.patient_id == "patient-A"
    assert (await store.get(cid)).patient_id == "patient-A"


# ---------------------------------------------------------------------------
# answer_followup: pinned records + prior turns -> GroundedAnswer
# ---------------------------------------------------------------------------


@respx.mock
async def test_answer_followup_sends_records_and_history_and_parses(
    settings: Settings,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_answer_body(_answer()))
    )

    history = [
        ConversationTurn(role="user", text="Give me the summary."),
        ConversationTurn(role="assistant", text="Penicillin allergy is the headline."),
    ]

    client = LLMClient(settings=settings, client=_anthropic())
    answer = await client.answer_followup(
        "What are her allergies?", history, _critical_set(), _deltas()
    )

    # Parsed into the typed, source-bound contract.
    assert isinstance(answer, GroundedAnswer)
    assert answer.answer[0].sources[0].id == "alg-1"
    assert answer.answer[0].is_grounded

    # Request shape: Sonnet, GroundedAnswer schema attached.
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "claude-sonnet-5"
    assert body["output_config"]["format"]["type"] == "json_schema"

    # The user payload carries the question, the prior turns, and the pinned
    # patient's retained records with their citable source ids.
    sent = json.loads(body["messages"][0]["content"])
    assert sent["question"] == "What are her allergies?"
    assert [t["role"] for t in sent["history"]] == ["user", "assistant"]
    assert sent["history"][1]["text"] == "Penicillin allergy is the headline."
    assert sent["records"]["allergies"][0]["source"] == {
        "resource_type": "AllergyIntolerance",
        "id": "alg-1",
    }
    assert sent["records"]["medications"][0]["id"] == "med-1"


def test_build_followup_payload_shape() -> None:
    payload = build_followup_payload(
        "her allergies?",
        [ConversationTurn(role="user", text="hi")],
        _critical_set(),
        _deltas(),
    )
    data = json.loads(payload)
    assert data["question"] == "her allergies?"
    assert data["history"] == [{"role": "user", "text": "hi"}]
    assert data["records"]["allergies"][0]["id"] == "alg-1"


@respx.mock
async def test_answer_followup_refusal_surfaces_typed_error(settings: Settings) -> None:
    from copilot.llm.client import LLMError

    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_answer_body(_answer(), stop_reason="refusal"))
    )

    client = LLMClient(settings=settings, client=_anthropic())
    with pytest.raises(LLMError) as excinfo:
        await client.answer_followup("q", [], _critical_set(), _deltas())
    assert excinfo.value.retriable is False
