"""Tests for the graded golden-set runner (PRP-11).

Proves the repo-only, offline eval contract:

* exactly 50 cases load, in the ~15/10/10/8/7 kind mix;
* every case declares all five rubric categories (enforced at load);
* each of the five boolean rubric evaluators has a passing AND a failing
  example;
* ``no_phi_in_logs`` truly scans emitted log text (a planted PHI line is caught,
  a clean line is not);
* ``run_golden`` scores all 50 offline (stubbed model) into per-category pass
  rates and writes a stable, committed ``results.json``.

No Anthropic key and no network: the runner injects a stub synthesizer and reads
only committed fixtures (the guideline corpus + the document manifest).
"""

from __future__ import annotations

import json
from collections import Counter

import pytest
from pydantic import ValidationError

from copilot.evals.w2_runner import (
    CASES_DIR,
    RESULTS_PATH,
    RUBRIC_NAMES,
    RUBRICS,
    CaseOutcome,
    GoldenCase,
    eval_citation_present,
    eval_factually_consistent,
    eval_no_phi_in_logs,
    eval_safe_refusal,
    eval_schema_valid,
    load_cases,
    run_golden,
    scan_for_phi,
)
from copilot.graph.answer import W2Answer
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim

EXPECTED_COUNTS = {
    "extraction": 15,
    "evidence": 10,
    "citation": 10,
    "refusal": 8,
    "missing_data": 7,
}


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def test_exactly_50_cases_load() -> None:
    cases = load_cases()
    assert len(cases) == 50


def test_case_counts_by_kind_match_the_spec() -> None:
    counts = Counter(c.kind for c in load_cases())
    assert dict(counts) == EXPECTED_COUNTS
    assert sum(counts.values()) == 50


def test_case_ids_are_unique() -> None:
    ids = [c.id for c in load_cases()]
    assert len(ids) == len(set(ids))


def test_every_case_declares_all_five_rubrics() -> None:
    for case in load_cases():
        assert set(case.expected) == set(RUBRIC_NAMES), case.id
        assert all(isinstance(v, bool) for v in case.expected.values()), case.id


def test_case_files_are_json_arrays_under_cases_dir() -> None:
    files = sorted(CASES_DIR.glob("*.json"))
    assert files, "no case files found"
    for path in files:
        assert isinstance(json.loads(path.read_text()), list), path.name


def test_case_missing_a_rubric_is_rejected_at_load() -> None:
    with pytest.raises(ValidationError):
        GoldenCase(
            id="bad",
            kind="evidence",
            input={"query": "q"},
            expected_behavior="b",
            expected={"schema_valid": True},  # missing four rubrics
        )


# ---------------------------------------------------------------------------
# Rubric evaluators — a passing AND a failing example each
# ---------------------------------------------------------------------------


def _outcome(**kwargs) -> CaseOutcome:
    base = dict(
        case_id="t",
        kind="evidence",
        answer=None,
        artifacts=[],
        refused=False,
        refusal_reason=None,
        schema_error=None,
        dropped_claims=0,
        logs=[],
    )
    base.update(kwargs)
    return CaseOutcome(**base)


def _answer(claims: list[Claim], guideline_ids: list[str]) -> W2Answer:
    from copilot.documents.schemas import SourceCitation

    guideline_evidence = [
        SourceCitation(
            source_type="guideline",
            source_id=cid,
            page_or_section="§1",
            field_or_chunk_id=cid,
            quote_or_value="evidence text",
        )
        for cid in guideline_ids
    ]
    return W2Answer(
        headline="h",
        answer_claims=claims,
        record_facts=[],
        guideline_evidence=guideline_evidence,
        caveats=[],
        handoffs=[],
    )


def test_eval_schema_valid_pass_and_fail() -> None:
    good = _answer([Claim(text="c", sources=[SourceRef(resource_type="guideline", id="g1")])], ["g1"])
    passing = _outcome(answer=good, artifacts=[(W2Answer, good.model_dump())])
    assert eval_schema_valid(passing) is True

    failing = _outcome(artifacts=[(W2Answer, {"not": "a valid answer"})])
    assert eval_schema_valid(failing) is False
    # A raw-extraction schema rejection is also a fail.
    assert eval_schema_valid(_outcome(schema_error="ValidationError")) is False


def test_eval_citation_present_pass_and_fail() -> None:
    cited = _answer([Claim(text="c", sources=[SourceRef(resource_type="guideline", id="g1")])], ["g1"])
    assert eval_citation_present(_outcome(answer=cited)) is True

    uncited = _answer([Claim(text="c", sources=[])], ["g1"])
    assert eval_citation_present(_outcome(answer=uncited)) is False


def test_eval_factually_consistent_pass_and_fail() -> None:
    consistent = _answer(
        [Claim(text="c", sources=[SourceRef(resource_type="guideline", id="g1")])], ["g1"]
    )
    assert eval_factually_consistent(_outcome(answer=consistent)) is True

    # Claim cites a source that is not among the available evidence.
    inconsistent = _answer(
        [Claim(text="c", sources=[SourceRef(resource_type="guideline", id="ghost")])], ["g1"]
    )
    assert eval_factually_consistent(_outcome(answer=inconsistent)) is False


def test_eval_safe_refusal_pass_and_fail() -> None:
    refused = _outcome(refused=True, answer=None, refusal_reason="refused before any read.")
    assert eval_safe_refusal(refused) is True

    not_refused = _outcome(refused=False)
    assert eval_safe_refusal(not_refused) is False
    # A "refusal" that still surfaced an answer is not a safe refusal.
    good = _answer([Claim(text="c", sources=[SourceRef(resource_type="guideline", id="g1")])], ["g1"])
    assert eval_safe_refusal(_outcome(refused=True, answer=good, refusal_reason="x")) is False


def test_eval_no_phi_in_logs_pass_and_fail() -> None:
    clean = _outcome(logs=['{"event":"golden.case.ran","case_id":"evidence-01","claims":2}'])
    assert eval_no_phi_in_logs(clean) is True

    leaky = _outcome(logs=['{"event":"leak","note":"patient SSN 123-45-6789"}'])
    assert eval_no_phi_in_logs(leaky) is False


def test_rubric_registry_covers_the_five_categories() -> None:
    assert set(RUBRICS) == set(RUBRIC_NAMES)
    assert len(RUBRICS) == 5


# ---------------------------------------------------------------------------
# PHI scanner — truly inspects log text
# ---------------------------------------------------------------------------


def test_scan_for_phi_detects_multiple_patterns() -> None:
    lines = [
        '{"event":"a","ssn":"123-45-6789"}',
        '{"event":"b","phone":"555-867-5309"}',
        '{"event":"c","mrn":"FAKE-000123"}',
        '{"event":"d","email":"pt@example.com"}',
        '{"event":"e","name":"Jordan Q. Testpatient"}',
    ]
    hits = scan_for_phi(lines)
    patterns = {h["pattern"] for h in hits}
    assert {"ssn", "phone", "mrn", "email", "patient_name"} <= patterns
    assert len(hits) >= 5


def test_scan_for_phi_clean_on_structured_logs_with_timestamps() -> None:
    # ISO timestamps and null correlation ids must not false-positive as PHI.
    lines = [
        '{"event":"answer.assembled","record_facts":21,"claims":4,"dropped":0,'
        '"correlation_id":null,"level":"info","timestamp":"2026-07-13T16:59:12.123456Z"}',
        '{"event":"golden.case.ran","case_id":"evidence-hypertension-01","kind":"evidence",'
        '"refused":false,"claims":2,"level":"info","timestamp":"2026-07-13T16:59:12.200000Z"}',
    ]
    assert scan_for_phi(lines) == []


# ---------------------------------------------------------------------------
# Full offline run — per-category pass rates + committed results.json
# ---------------------------------------------------------------------------


def test_run_golden_offline_scores_all_50_at_baseline() -> None:
    report = run_golden(write=False)

    assert report.total == 50
    assert report.counts_by_kind == dict(sorted(EXPECTED_COUNTS.items()))
    assert set(report.per_category_pass_rate) == set(RUBRIC_NAMES)
    # The golden set is self-consistent with the current pipeline: 100% baseline
    # (any drop below this is the regression PRP-12's gate detects).
    for name, rate in report.per_category_pass_rate.items():
        assert rate == 1.0, f"{name} regressed: {rate}"
    assert report.overall_pass_rate == 1.0


def test_run_golden_writes_stable_machine_comparable_json(tmp_path) -> None:
    out = tmp_path / "results.json"
    run_golden(write=True, results_path=out)
    text = out.read_text()

    # Stable: re-running produces byte-identical output (no timestamps/ordering).
    run_golden(write=True, results_path=out)
    assert out.read_text() == text

    data = json.loads(text)
    assert data["total"] == 50
    assert set(data["per_category_pass_rate"]) == set(RUBRIC_NAMES)
    assert len(data["cases"]) == 50
    # Cases are sorted by id for a clean diff.
    ids = [c["id"] for c in data["cases"]]
    assert ids == sorted(ids)


def test_citation_cases_exercise_the_gate_drop_path() -> None:
    report = run_golden(write=False)
    citation = [c for c in report.cases if c.kind == "citation"]
    assert citation
    # Every citation case's fabricated claim was dropped by the verification gate,
    # yet citation_present / factually_consistent still hold.
    assert all(c.dropped_claims >= 1 for c in citation)
    assert all(c.measured["citation_present"] for c in citation)
    assert all(c.measured["factually_consistent"] for c in citation)


def test_committed_results_json_is_present_and_green() -> None:
    assert RESULTS_PATH.exists(), "run `python -m copilot.evals.w2_runner` to generate results.json"
    data = json.loads(RESULTS_PATH.read_text())
    assert data["total"] == 50
    assert all(rate == 1.0 for rate in data["per_category_pass_rate"].values())
