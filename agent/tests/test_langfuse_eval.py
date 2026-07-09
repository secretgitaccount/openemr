"""Unit tests for the Langfuse eval's pure pieces (no network / no LLM).

The scoring logic (`_outcome`, `refusal_correct`, `grounding_holds`) is
deterministic and must be correct independent of the live run, so it is tested
here with lightweight stand-ins for a `PatientSummary`.
"""

from __future__ import annotations

from types import SimpleNamespace

from copilot.evals.langfuse_eval import (
    _outcome,
    grounding_holds,
    refusal_correct,
)


def _claim(n_sources: int):
    return SimpleNamespace(sources=list(range(n_sources)))


def _granted(must_knows, whats_changed=(), labs_omitted=0):
    summary = SimpleNamespace(must_knows=list(must_knows), whats_changed=list(whats_changed))
    verified = SimpleNamespace(summary=summary)
    return SimpleNamespace(refused=False, verified=verified, labs_omitted=labs_omitted)


def _refused(reason="out of panel"):
    return SimpleNamespace(refused=True, decision=SimpleNamespace(reason=reason), labs_omitted=0)


# --- _outcome ---------------------------------------------------------------


def test_outcome_refused_reports_no_summary():
    out = _outcome(_refused("nope"))
    assert out == {
        "refused": True,
        "summary_produced": False,
        "num_claims": 0,
        "all_claims_sourced": None,
        "reason": "nope",
    }


def test_outcome_granted_all_claims_sourced():
    out = _outcome(_granted([_claim(1), _claim(2)], [_claim(1)]))
    assert out["refused"] is False
    assert out["summary_produced"] is True
    assert out["num_claims"] == 3
    assert out["all_claims_sourced"] is True


def test_outcome_granted_detects_ungrounded_claim():
    out = _outcome(_granted([_claim(1), _claim(0)]))
    assert out["all_claims_sourced"] is False


# --- refusal_correct --------------------------------------------------------


def test_refusal_correct_matches():
    ev = refusal_correct(output={"refused": True}, expected_output={"refused": True})
    assert ev.value is True


def test_refusal_correct_mismatch():
    ev = refusal_correct(
        output={"refused": False}, expected_output={"refused": True}
    )
    assert ev.value is False


# --- grounding_holds --------------------------------------------------------


def test_grounding_holds_pass_when_grounded_and_sourced():
    ev = grounding_holds(
        output={"refused": False, "all_claims_sourced": True, "num_claims": 3},
        expected_output={"expect_grounded": True},
    )
    assert ev.value is True


def test_grounding_holds_fail_on_ungrounded_summary():
    ev = grounding_holds(
        output={"refused": False, "all_claims_sourced": False, "num_claims": 3},
        expected_output={"expect_grounded": True},
    )
    assert ev.value is False


def test_grounding_holds_vacuous_on_refusal_path():
    ev = grounding_holds(
        output={"refused": True, "summary_produced": False},
        expected_output={"expect_grounded": False},
    )
    assert ev.value is True
