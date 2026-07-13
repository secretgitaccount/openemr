"""Tests for PHI-scrubbed Langfuse observability wiring (PRP M0-6)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from copilot import observability
from copilot.observability import (
    REDACTED,
    EncounterMetrics,
    StepLatency,
    WorkerLatency,
    estimate_cost_usd,
    record_encounter_metrics,
    record_tool_result,
    record_verification,
    scrub_phi,
    trace,
)


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    """Ensure each test starts from a clean client cache."""

    observability.reset_langfuse_client()
    yield
    observability.reset_langfuse_client()


@pytest.fixture
def _no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the "unconfigured" path regardless of real keys in the env.

    These disabled-path tests must assert graceful degradation independent of
    whatever ``.env`` the developer has locally (real Langfuse keys must not
    make them fail), so pin placeholder keys for the duration of the test.
    """

    from copilot.config import get_settings as _real_get_settings

    placeholder = _real_get_settings().model_copy(
        update={"langfuse_public_key": "pk-xxxx", "langfuse_secret_key": "sk-xxxx"}
    )
    monkeypatch.setattr(observability, "get_settings", lambda: placeholder)
    observability.reset_langfuse_client()


# ---------------------------------------------------------------------------
# scrub_phi
# ---------------------------------------------------------------------------


def test_scrub_keeps_ids_redacts_clinical_values() -> None:
    scrubbed = scrub_phi({"patient_id": "123", "lab_value": "K 5.9"})
    assert scrubbed == {"patient_id": "123", "lab_value": REDACTED}


def test_scrub_keeps_resource_type_and_timestamps() -> None:
    scrubbed = scrub_phi(
        {
            "resourceType": "Observation",
            "id": "obs-1",
            "status": "final",
            "effective_date": "2026-07-01",
            "value": "Potassium 5.9 mmol/L",
            "note": "patient reports fatigue",
        }
    )
    assert scrubbed["resourceType"] == "Observation"
    assert scrubbed["id"] == "obs-1"
    assert scrubbed["status"] == "final"
    assert scrubbed["effective_date"] == "2026-07-01"
    assert scrubbed["value"] == REDACTED
    assert scrubbed["note"] == REDACTED


def test_scrub_is_recursive_and_handles_lists() -> None:
    scrubbed = scrub_phi(
        {
            "encounter_id": "enc-9",
            "labs": [
                {"source_id": "lab-1", "result": "5.9"},
                {"source_id": "lab-2", "result": "3.1"},
            ],
            "raw_values": ["K 5.9", "Na 140"],
        }
    )
    assert scrubbed["encounter_id"] == "enc-9"
    assert scrubbed["labs"][0]["source_id"] == "lab-1"
    assert scrubbed["labs"][0]["result"] == REDACTED
    assert scrubbed["labs"][1]["result"] == REDACTED
    # scalars nested in a list under an unsafe key are still redacted
    assert scrubbed["raw_values"] == [REDACTED, REDACTED]


def test_scrub_does_not_mutate_input() -> None:
    original = {"patient_id": "123", "lab_value": "K 5.9"}
    scrub_phi(original)
    assert original == {"patient_id": "123", "lab_value": "K 5.9"}


def test_scrub_handles_pydantic_models() -> None:
    from pydantic import BaseModel

    class Lab(BaseModel):
        source_id: str
        value: str

    scrubbed = scrub_phi(Lab(source_id="lab-7", value="K 5.9"))
    assert scrubbed == {"source_id": "lab-7", "value": REDACTED}


def test_scrub_preserves_none_and_empty_containers() -> None:
    scrubbed = scrub_phi({"patient_id": None, "labs": [], "meta": {}})
    assert scrubbed == {"patient_id": None, "labs": [], "meta": {}}


# ---------------------------------------------------------------------------
# Graceful degradation (no keys -> no-op)
# ---------------------------------------------------------------------------


def test_client_is_none_with_placeholder_keys(_no_keys: None) -> None:
    # Placeholder ("xxxx") Langfuse keys => no client.
    assert observability.get_langfuse_client() is None
    assert observability.langfuse_enabled() is False


def test_trace_is_noop_without_keys(_no_keys: None) -> None:
    ran = {"body": False}
    with trace("noop-span") as span:
        assert span.enabled is False
        ran["body"] = True
        # methods on a disabled span are safe no-ops
        span.update(output={"lab_value": "K 5.9"})
        span.event("something")
    assert ran["body"] is True


def test_trace_works_as_decorator_without_keys() -> None:
    calls = {"n": 0}

    @trace("decorated")
    def do_work() -> str:
        calls["n"] += 1
        return "ok"

    assert do_work() == "ok"
    assert calls["n"] == 1


def test_event_helpers_are_noop_without_keys() -> None:
    # Should simply do nothing and never raise.
    record_verification(True)
    record_verification(False, reason="unsupported claim")
    record_tool_result("get_active_medications", True)
    record_tool_result("get_recent_labs", False)


# ---------------------------------------------------------------------------
# Tracing with a mocked Langfuse client
# ---------------------------------------------------------------------------


def _install_mock_client(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Wire a mock Langfuse client and return (client, span) mocks."""

    span = MagicMock(name="span")
    cm = MagicMock(name="observation_cm")
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False

    client = MagicMock(name="langfuse")
    client.start_as_current_observation.return_value = cm

    monkeypatch.setattr(observability, "get_langfuse_client", lambda: client)
    return client, span


def test_trace_tags_span_with_correlation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, span = _install_mock_client(monkeypatch)
    monkeypatch.setattr(observability, "_current_correlation_id", lambda: "corr-abc-123")

    with trace("fetch-labs", input={"patient_id": "p1", "lab_value": "K 5.9"}) as handle:
        assert handle.enabled is True

    client.start_as_current_observation.assert_called_once()
    kwargs = client.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "fetch-labs"
    # correlation id is tagged in the span metadata
    assert kwargs["metadata"]["correlation_id"] == "corr-abc-123"
    # input PHI is scrubbed before it reaches Langfuse
    assert kwargs["input"]["patient_id"] == "p1"
    assert kwargs["input"]["lab_value"] == REDACTED

    # success + duration recorded on exit
    update_kwargs = span.update.call_args.kwargs
    assert update_kwargs["metadata"]["success"] is True
    assert "duration_ms" in update_kwargs["metadata"]
    assert update_kwargs["metadata"]["correlation_id"] == "corr-abc-123"


def test_trace_records_failure_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    client, span = _install_mock_client(monkeypatch)
    monkeypatch.setattr(observability, "_current_correlation_id", lambda: "corr-err")

    with pytest.raises(ValueError):
        with trace("boom"):
            raise ValueError("secret PHI in message")

    update_kwargs = span.update.call_args.kwargs
    assert update_kwargs["metadata"]["success"] is False
    assert update_kwargs["level"] == "ERROR"
    # exception *type* recorded, not its (possibly PHI-bearing) message
    assert update_kwargs["status_message"] == "ValueError"
    assert "secret PHI" not in str(update_kwargs)


def test_trace_scrubs_output_via_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, span = _install_mock_client(monkeypatch)
    monkeypatch.setattr(observability, "_current_correlation_id", lambda: None)

    with trace("with-output") as handle:
        handle.update(output={"source_id": "lab-1", "result": "K 5.9"})

    outputs = [
        c.kwargs["output"]
        for c in span.update.call_args_list
        if "output" in c.kwargs
    ]
    assert outputs, "expected an output update"
    assert outputs[0] == {"source_id": "lab-1", "result": REDACTED}


# ---------------------------------------------------------------------------
# Event helpers with a mocked client
# ---------------------------------------------------------------------------


def test_record_verification_emits_scrubbed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _span = _install_mock_client(monkeypatch)
    monkeypatch.setattr(observability, "_current_correlation_id", lambda: "corr-v")

    record_verification(False, reason="claim not grounded in record", metadata={"claim_id": "c1"})

    client.create_event.assert_called_once()
    kwargs = client.create_event.call_args.kwargs
    assert kwargs["name"] == "verification.fail"
    md = kwargs["metadata"]
    assert md["passed"] is False
    assert md["claim_id"] == "c1"
    assert md["correlation_id"] == "corr-v"
    # free-text reason is redacted (never trusted to be PHI-free)
    assert md["reason"] == REDACTED


def test_record_tool_result_emits_event(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _span = _install_mock_client(monkeypatch)
    monkeypatch.setattr(observability, "_current_correlation_id", lambda: "corr-t")

    record_tool_result("get_recent_labs", True, metadata={"count": 3})

    client.create_event.assert_called_once()
    kwargs = client.create_event.call_args.kwargs
    assert kwargs["name"] == "tool.success"
    assert kwargs["metadata"]["tool"] == "get_recent_labs"
    assert kwargs["metadata"]["success"] is True
    assert kwargs["metadata"]["count"] == 3


# ---------------------------------------------------------------------------
# Cost estimate (PRP-14)
# ---------------------------------------------------------------------------


def test_estimate_cost_sonnet_input_and_output() -> None:
    # 1M input @ $3, 1M output @ $15 → $18.00 for claude-sonnet-5.
    assert estimate_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000) == 18.0
    # Pure input pricing.
    assert estimate_cost_usd("claude-sonnet-5", 1_000_000, 0) == 3.0


def test_estimate_cost_unknown_model_falls_back_to_sonnet() -> None:
    assert estimate_cost_usd("some-future-model", 1_000_000, 0) == 3.0
    assert estimate_cost_usd(None, 0, 0) == 0.0


def test_estimate_cost_scales_with_small_token_counts() -> None:
    # 12k in / 3k out on Sonnet: 12000/1e6*3 + 3000/1e6*15 = 0.036 + 0.045.
    assert estimate_cost_usd("claude-sonnet-5", 12_000, 3_000) == round(0.036 + 0.045, 6)


# ---------------------------------------------------------------------------
# Per-encounter metrics (PRP-14, FR-9) — emitted, correlation-tagged, PHI-free
# ---------------------------------------------------------------------------


def _sample_metrics() -> EncounterMetrics:
    """A realistic per-encounter metrics object built from structural data only."""

    return EncounterMetrics(
        correlation_id="corr-enc-1",
        patient_id="pat-123",
        tool_sequence=["intake_extractor", "evidence_retriever", "answer.synthesize"],
        step_latencies=[
            StepLatency(step="ingestion", latency_ms=812.5),
            StepLatency(step="retrieval", latency_ms=140.2),
        ],
        total_latency_ms=1421.7,
        worker_latencies=[
            WorkerLatency(worker="intake_extractor", latency_ms=812.5, success=True),
            WorkerLatency(worker="evidence_retriever", latency_ms=140.2, success=True),
        ],
        routing_decisions=[
            "supervisor->intake_extractor",
            "supervisor->evidence_retriever",
            "supervisor->done",
        ],
        steps=3,
        model="claude-sonnet-5",
        input_tokens=1200,
        output_tokens=340,
        cost_usd=estimate_cost_usd("claude-sonnet-5", 1200, 340),
        retrieval_hit_rate=0.75,
        extraction_confidence=0.91,
        claims=4,
        dropped_claims=1,
        eval_outcome="pass",
    )


def test_encounter_metrics_emitted_with_correlation_and_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _span = _install_mock_client(monkeypatch)
    # Metrics carry their own correlation id; the current-context id differs so we
    # prove the metrics' id wins (the graph-run root, not an ambient one).
    monkeypatch.setattr(observability, "_current_correlation_id", lambda: "ambient-id")

    record_encounter_metrics(_sample_metrics())

    client.create_event.assert_called_once()
    kwargs = client.create_event.call_args.kwargs
    assert kwargs["name"] == "encounter.metrics"
    md = kwargs["metadata"]

    # Correlation-ID root: the metrics' own id nests this under the graph run.
    assert md["correlation_id"] == "corr-enc-1"

    # Every structural metric survived the scrub (nothing redacted).
    assert md["tool_sequence"] == [
        "intake_extractor",
        "evidence_retriever",
        "answer.synthesize",
    ]
    assert md["routing_decisions"][0] == "supervisor->intake_extractor"
    assert md["step_latencies"][0] == {"step": "ingestion", "latency_ms": 812.5}
    assert md["worker_latencies"][0]["worker"] == "intake_extractor"
    assert md["total_latency_ms"] == 1421.7
    assert md["model"] == "claude-sonnet-5"
    assert md["input_tokens"] == 1200
    assert md["output_tokens"] == 340
    assert md["cost_usd"] == estimate_cost_usd("claude-sonnet-5", 1200, 340)
    assert md["retrieval_hit_rate"] == 0.75
    assert md["extraction_confidence"] == 0.91
    assert md["claims"] == 4
    assert md["dropped_claims"] == 1
    assert md["eval_outcome"] == "pass"
    assert md["patient_id"] == "pat-123"  # a record id is kept


def test_encounter_metrics_payload_carries_no_phi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The emitted metrics payload must contain no clinical values (NFR-4).

    The metrics model is structural by construction; this asserts the emitted
    payload contains none of a set of PHI sentinels, and — separately — that the
    scrubber still fails closed on an unexpected clinical key even after the
    metric keys were added to the allowlist.
    """

    client, _span = _install_mock_client(monkeypatch)
    monkeypatch.setattr(observability, "_current_correlation_id", lambda: None)

    record_encounter_metrics(_sample_metrics())

    payload = repr(client.create_event.call_args.kwargs["metadata"])
    for phi in ("K 5.9", "mmol/L", "Potassium", "fatigue", "Jane", "Doe"):
        assert phi not in payload

    # Fail-closed proof: a clinical value under an unexpected key is still
    # redacted (widening the allowlist for metric keys did not open a hole).
    scrubbed = scrub_phi(
        {"step": "retrieval", "latency_ms": 12.0, "note": "K 5.9 mmol/L"}
    )
    assert scrubbed["step"] == "retrieval"  # allowlisted metric key kept
    assert scrubbed["latency_ms"] == 12.0
    assert scrubbed["note"] == REDACTED  # unknown key → redacted


def test_encounter_metrics_noop_without_keys(_no_keys: None) -> None:
    # No client configured → emits nothing, never raises.
    record_encounter_metrics(_sample_metrics())


# ---------------------------------------------------------------------------
# Graph spans nest under a single correlation-ID root (PRP-14 / NFR-2)
# ---------------------------------------------------------------------------


def test_graph_spans_nest_under_correlation_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor span and a worker span opened inside it both use the
    Langfuse current-observation nesting primitive and carry the same
    correlation id (the run's root)."""

    client, _span = _install_mock_client(monkeypatch)
    monkeypatch.setattr(observability, "_current_correlation_id", lambda: "run-root-1")

    with trace("graph.supervisor", as_type="span"):
        # A worker span opened while the supervisor observation is current nests
        # under it (start_as_current_observation uses the active observation).
        with trace("graph.intake_extractor", as_type="span"):
            pass

    # Both spans went through the nesting primitive, in order.
    names = [
        c.kwargs["name"] for c in client.start_as_current_observation.call_args_list
    ]
    assert names == ["graph.supervisor", "graph.intake_extractor"]
    # Both are tagged with the same correlation-ID root.
    for c in client.start_as_current_observation.call_args_list:
        assert c.kwargs["metadata"]["correlation_id"] == "run-root-1"
