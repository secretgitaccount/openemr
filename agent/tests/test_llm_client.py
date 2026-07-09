"""Unit tests for the grounded-summary LLM client (PRP M1-5).

The Anthropic HTTP is mocked with ``respx`` (the async httpx transport is
intercepted globally); the live end-to-end proof is the
``python -m copilot.llm.client --patient <id>`` gate in the PRP's Validation.
No ANTHROPIC_API_KEY is needed here.

Coverage:

* the request targets ``claude-sonnet-5``, attaches the ``GroundedSummary``
  JSON schema (``output_config.format``), threads the correlation id, and sends
  only the minimum-necessary payload (no clinical values without a source);
* a mocked response parses into a :class:`GroundedSummary` with source-bound
  claims;
* a mocked refusal surfaces a typed :class:`LLMError` (no fabrication);
* an unparseable / schema-violating response surfaces a typed error;
* a transient 5xx is retried then succeeds;
* a missing key is surfaced (clearly) only when a live call is attempted, not
  at import.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from anthropic import AsyncAnthropic

from copilot.config import Settings
from copilot.llm.client import LLMClient, LLMError, build_payload
from copilot.logging import (
    CORRELATION_ID_HEADER,
    reset_correlation_id,
    set_correlation_id,
)
from copilot.schemas.clinical import CriticalSet, Deltas, LabResult, Medication
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

MESSAGES_URL = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    # A real-looking key so the injected-client path is exercised without one.
    return Settings(anthropic_api_key="sk-ant-real", anthropic_model="claude-sonnet-5")


def _anthropic() -> AsyncAnthropic:
    # max_retries=0 so the SDK's own retry doesn't mask the client's tenacity.
    return AsyncAnthropic(api_key="test-key", max_retries=0)


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
        missing=["allergies"],
    )


def _deltas() -> Deltas:
    return Deltas(
        new_meds=[
            Medication(
                id="med-2",
                name="Metformin",
                status="active",
                source=SourceRef(resource_type="MedicationRequest", id="med-2"),
            )
        ]
    )


def _summary() -> GroundedSummary:
    return GroundedSummary(
        headline="Critically high potassium (K 6.1)",
        must_knows=[
            Claim(
                text="Potassium is 6.1 mmol/L (abnormal).",
                sources=[SourceRef(resource_type="Observation", id="lab-1")],
            )
        ],
        whats_changed=[
            Claim(
                text="Metformin started since last visit.",
                sources=[SourceRef(resource_type="MedicationRequest", id="med-2")],
            )
        ],
        caveats=["No allergy data on file."],
    )


def _message_body(
    summary: GroundedSummary | None,
    *,
    stop_reason: str = "end_turn",
) -> dict:
    content = []
    if summary is not None:
        content.append({"type": "text", "text": summary.model_dump_json()})
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 45},
    }


def _raw_text_body(text: str) -> dict:
    return {
        "id": "msg_2",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


# ---------------------------------------------------------------------------
# Request shape + parsing
# ---------------------------------------------------------------------------


@respx.mock
async def test_summarize_targets_sonnet_attaches_schema_and_threads_cid(
    settings: Settings,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_message_body(_summary()))
    )

    token = set_correlation_id("corr-xyz")
    try:
        client = LLMClient(settings=settings, client=_anthropic())
        summary = await client.summarize(_critical_set(), _deltas())
    finally:
        reset_correlation_id(token)

    # Parsed into the typed contract with source-bound claims.
    assert isinstance(summary, GroundedSummary)
    assert summary.headline
    assert summary.must_knows[0].sources[0].id == "lab-1"
    assert summary.must_knows[0].is_grounded

    # Request shape: Sonnet, schema attached, correlation id threaded.
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "schema" in body["output_config"]["format"]
    assert request.headers.get(CORRELATION_ID_HEADER) == "corr-xyz"


@respx.mock
async def test_payload_is_minimum_necessary_and_carries_source_ids(
    settings: Settings,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_message_body(_summary()))
    )

    client = LLMClient(settings=settings, client=_anthropic())
    await client.summarize(_critical_set(), _deltas())

    body = json.loads(route.calls.last.request.content)
    user_content = body["messages"][0]["content"]
    sent = json.loads(user_content)
    # Records carry citable source pointers and clinical values.
    assert sent["labs"][0]["source"] == {"resource_type": "Observation", "id": "lab-1"}
    assert sent["labs"][0]["value"] == "6.1"
    assert sent["missing"] == ["allergies"]
    assert sent["deltas"]["new_meds"][0]["id"] == "med-2"


def test_build_payload_is_json_and_omits_absent_timestamps() -> None:
    payload = build_payload(_critical_set(), _deltas())
    data = json.loads(payload)
    # No timestamp on the source (none supplied) -> key absent, not null.
    assert "timestamp" not in data["labs"][0]["source"]


# ---------------------------------------------------------------------------
# Failure surfaces (FR-11: surface, don't fabricate)
# ---------------------------------------------------------------------------


@respx.mock
async def test_refusal_surfaces_typed_error(settings: Settings) -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_message_body(None, stop_reason="refusal"))
    )

    client = LLMClient(settings=settings, client=_anthropic())
    with pytest.raises(LLMError) as excinfo:
        await client.summarize(_critical_set(), _deltas())
    assert excinfo.value.retriable is False


@respx.mock
async def test_schema_violation_surfaces_typed_error(settings: Settings) -> None:
    # Valid JSON, but missing the required `headline` -> ValidationError inside parse.
    bad = json.dumps({"must_knows": [], "whats_changed": [], "caveats": []})
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=_raw_text_body(bad)))

    client = LLMClient(settings=settings, client=_anthropic())
    with pytest.raises(LLMError) as excinfo:
        await client.summarize(_critical_set(), _deltas())
    assert excinfo.value.retriable is False


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


@respx.mock
async def test_transient_5xx_is_retried_then_succeeds(settings: Settings) -> None:
    route = respx.post(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(503, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
            httpx.Response(200, json=_message_body(_summary())),
        ]
    )

    client = LLMClient(settings=settings, client=_anthropic())
    summary = await client.summarize(_critical_set(), _deltas())

    assert isinstance(summary, GroundedSummary)
    assert route.call_count == 2


@respx.mock
async def test_permanent_4xx_is_not_retried(settings: Settings) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
        )
    )

    client = LLMClient(settings=settings, client=_anthropic())
    with pytest.raises(LLMError) as excinfo:
        await client.summarize(_critical_set(), _deltas())
    assert excinfo.value.retriable is False
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Missing key: surfaced on use, not at import
# ---------------------------------------------------------------------------


async def test_missing_key_surfaced_only_on_live_call() -> None:
    # Placeholder key + no injected client -> a clear error when a call is tried.
    client = LLMClient(settings=Settings(anthropic_api_key="sk-ant-xxxxxxxx"))
    with pytest.raises(LLMError) as excinfo:
        await client.summarize(_critical_set(), _deltas())
    assert excinfo.value.retriable is False
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
