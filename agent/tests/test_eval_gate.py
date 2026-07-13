"""Tests for the PR-blocking eval regression gate (PRP-12, FR-8).

Proves the graded contract:

* the committed baseline covers exactly the five rubrics and loads;
* a fresh golden run holds green against the committed baseline (exit 0);
* a >5% absolute drop in any category is flagged with old->new numbers;
* a drop below the pass threshold is flagged even within tolerance;
* ``run_gate`` returns a nonzero exit code the moment a category regresses.

All offline: :func:`run_golden` uses a stubbed model and committed fixtures, so
this needs no Anthropic key and no network.
"""

from __future__ import annotations

import json

import pytest

from copilot.evals.gate import (
    BASELINE_PATH,
    Baseline,
    check_regressions,
    load_baseline,
    run_gate,
    write_baseline,
)
from copilot.evals.w2_runner import RUBRIC_NAMES, run_golden


def _report():
    return run_golden(write=False)


# ---------------------------------------------------------------------------
# Baseline loading
# ---------------------------------------------------------------------------


def test_committed_baseline_loads_and_covers_five_rubrics() -> None:
    baseline = load_baseline()
    assert set(baseline.per_category_pass_rate) == set(RUBRIC_NAMES)
    assert 0.0 <= baseline.threshold <= 1.0
    assert baseline.tolerance == pytest.approx(0.05)


def test_committed_baseline_json_is_all_green() -> None:
    data = json.loads(BASELINE_PATH.read_text())
    assert data["total"] == 50
    assert set(data["per_category_pass_rate"]) == set(RUBRIC_NAMES)
    assert all(rate == 1.0 for rate in data["per_category_pass_rate"].values())


def test_baseline_missing_a_rubric_is_rejected(tmp_path) -> None:
    bad = tmp_path / "baseline.json"
    bad.write_text(json.dumps({"per_category_pass_rate": {"schema_valid": 1.0}}))
    with pytest.raises(ValueError):
        load_baseline(bad)


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


def test_fresh_run_holds_green_against_committed_baseline() -> None:
    baseline = load_baseline()
    report = _report()
    assert check_regressions(report, baseline) == []


def test_five_percent_absolute_drop_is_flagged_with_old_new() -> None:
    baseline = load_baseline()
    report = _report()
    # factually_consistent falls 1.0 -> 0.80 (a 20% absolute drop).
    dropped = report.model_copy(
        update={
            "per_category_pass_rate": {**report.per_category_pass_rate, "factually_consistent": 0.80}
        }
    )
    regressions = check_regressions(dropped, baseline)
    assert [r.category for r in regressions] == ["factually_consistent"]
    reg = regressions[0]
    assert reg.baseline == 1.0
    assert reg.current == pytest.approx(0.80)
    assert "absolute drop" in reg.reason
    assert "1.0000 -> 0.8000" in reg.describe()


def test_drop_within_tolerance_is_not_flagged() -> None:
    baseline = load_baseline()
    report = _report()
    # A 4% drop (< 5% tolerance) and still above the 0.90 floor -> green.
    ok = report.model_copy(
        update={
            "per_category_pass_rate": {**report.per_category_pass_rate, "citation_present": 0.96}
        }
    )
    assert check_regressions(ok, baseline) == []


def test_below_pass_threshold_is_flagged_even_within_tolerance() -> None:
    # A baseline whose category sits just above the floor; a small drop crosses it.
    baseline = Baseline(
        per_category_pass_rate=dict.fromkeys(RUBRIC_NAMES, 0.92),
        tolerance=0.05,
        threshold=0.90,
    )
    report = _report()
    below = report.model_copy(
        update={"per_category_pass_rate": {**report.per_category_pass_rate, "safe_refusal": 0.89}}
    )
    regressions = check_regressions(below, baseline)
    assert [r.category for r in regressions] == ["safe_refusal"]
    assert "threshold" in regressions[0].reason


# ---------------------------------------------------------------------------
# Process exit code (what the hook / CI read)
# ---------------------------------------------------------------------------


def test_run_gate_exits_zero_against_committed_baseline() -> None:
    assert run_gate() == 0


def test_run_gate_exits_nonzero_when_baseline_demands_more(tmp_path) -> None:
    # A baseline that expects a category the current pipeline cannot reach makes
    # the gate go red — proving the exit code blocks a regression.
    unreachable = {
        "per_category_pass_rate": dict.fromkeys(RUBRIC_NAMES, 1.0),
        "pass_threshold": 0.90,
        "regression_tolerance": 0.05,
    }
    # Force a red by making one category's floor impossible via a doctored file.
    unreachable["pass_threshold"] = 1.01  # nothing can be >= 1.01
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(unreachable))
    assert run_gate(path) == 1


def test_write_baseline_snapshots_a_clean_run(tmp_path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"pass_threshold": 0.9, "regression_tolerance": 0.05,
                                "per_category_pass_rate": dict.fromkeys(RUBRIC_NAMES, 1.0)}))
    report = write_baseline(path)
    data = json.loads(path.read_text())
    assert data["total"] == report.total == 50
    assert set(data["per_category_pass_rate"]) == set(RUBRIC_NAMES)
