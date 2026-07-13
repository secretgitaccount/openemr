"""PR-blocking eval regression gate (PRP-12, FR-8).

The **graded hard gate**: run the deterministic golden set fresh
(:func:`copilot.evals.w2_runner.run_golden`, no key / no network) and compare
its per-category pass rates against a **committed baseline**
(``tests/eval/golden/baseline.json``). The build **fails** (nonzero exit) if any
rubric category:

* drops **more than 5% absolute** below its baseline rate, **or**
* falls **below its pass threshold** (an absolute floor, default 0.90).

On failure the offending categories are printed with their ``old -> new``
numbers and the reason, so a reviewer sees exactly which rubric regressed. A
clean run prints the per-category comparison and exits 0.

Run it (this is what the ``.githooks/pre-push`` hook and ``make ci`` invoke)::

    cd agent && .venv/bin/python -m copilot.evals.gate

The baseline is regenerated **deliberately** — never automatically in the hook —
after an intentional, reviewed change to the golden set or pipeline::

    cd agent && .venv/bin/python -m copilot.evals.gate --update-baseline
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from copilot.evals.w2_runner import GOLDEN_DIR, RUBRIC_NAMES, EvalReport, run_golden

__all__ = [
    "BASELINE_PATH",
    "DEFAULT_TOLERANCE",
    "DEFAULT_THRESHOLD",
    "Baseline",
    "Regression",
    "load_baseline",
    "check_regressions",
    "run_gate",
    "write_baseline",
    "main",
]

#: The committed baseline snapshot the gate compares a fresh run against.
BASELINE_PATH: Path = GOLDEN_DIR / "baseline.json"

#: Max allowed absolute drop in a category's pass rate before it is a regression.
DEFAULT_TOLERANCE = 0.05

#: Absolute pass-rate floor a category must stay at or above regardless of drop.
DEFAULT_THRESHOLD = 0.90


@dataclass(frozen=True)
class Baseline:
    """The committed baseline: per-category rates plus the gate's thresholds."""

    per_category_pass_rate: dict[str, float]
    tolerance: float
    threshold: float

    def threshold_for(self, category: str) -> float:
        """Per-category floor, allowing either a scalar or a per-category map."""

        # `threshold` is a scalar here; kept as a method so a future per-category
        # map (dict) can override without changing call sites.
        return self.threshold


@dataclass(frozen=True)
class Regression:
    """One category that failed the gate, with the numbers a reviewer needs."""

    category: str
    baseline: float
    current: float
    reason: str

    def describe(self) -> str:
        return f"{self.category}: {self.baseline:.4f} -> {self.current:.4f} ({self.reason})"


def load_baseline(path: Path = BASELINE_PATH) -> Baseline:
    """Load and parse the committed baseline snapshot.

    Fails closed: a missing or malformed baseline raises, which the caller
    surfaces as a nonzero exit — the gate never silently passes without a
    reference to compare against.
    """

    raw = json.loads(path.read_text())
    rates = raw["per_category_pass_rate"]
    if set(rates) != set(RUBRIC_NAMES):
        missing = sorted(set(RUBRIC_NAMES) - set(rates))
        extra = sorted(set(rates) - set(RUBRIC_NAMES))
        raise ValueError(
            f"baseline per_category_pass_rate must cover exactly the five rubrics; "
            f"missing={missing} extra={extra}"
        )
    return Baseline(
        per_category_pass_rate={name: float(rates[name]) for name in RUBRIC_NAMES},
        tolerance=float(raw.get("regression_tolerance", DEFAULT_TOLERANCE)),
        threshold=float(raw.get("pass_threshold", DEFAULT_THRESHOLD)),
    )


def check_regressions(report: EvalReport, baseline: Baseline) -> list[Regression]:
    """Return every category that regressed vs ``baseline`` (empty == green).

    A category regresses when its fresh rate drops more than ``tolerance``
    absolute below the baseline rate, **or** falls below the pass threshold.
    """

    regressions: list[Regression] = []
    for name in RUBRIC_NAMES:
        base = baseline.per_category_pass_rate[name]
        current = float(report.per_category_pass_rate.get(name, 0.0))
        floor = baseline.threshold_for(name)
        # Guard against float noise so an exact 5.00% drop isn't flagged.
        if current < base - baseline.tolerance - 1e-9:
            regressions.append(
                Regression(
                    category=name,
                    baseline=base,
                    current=current,
                    reason=f">{baseline.tolerance:.0%} absolute drop",
                )
            )
        elif current < floor - 1e-9:
            regressions.append(
                Regression(
                    category=name,
                    baseline=base,
                    current=current,
                    reason=f"below pass threshold {floor:.2f}",
                )
            )
    return regressions


def _print_comparison(report: EvalReport, baseline: Baseline) -> None:
    print(
        f"eval regression gate: fresh golden run vs committed baseline "
        f"(tolerance={baseline.tolerance:.0%} absolute, floor={baseline.threshold:.2f})"
    )
    for name in RUBRIC_NAMES:
        base = baseline.per_category_pass_rate[name]
        current = float(report.per_category_pass_rate.get(name, 0.0))
        delta = current - base
        mark = "OK" if current >= base - baseline.tolerance - 1e-9 and current >= baseline.threshold_for(name) - 1e-9 else "REGRESSION"
        print(f"  {name:<22} {base:.4f} -> {current:.4f}  ({delta:+.4f})  {mark}")


def run_gate(baseline_path: Path = BASELINE_PATH) -> int:
    """Run the golden set fresh, compare to baseline, return a process exit code.

    Returns ``0`` when every category holds and ``1`` when any regressed (naming
    the offending categories with old->new numbers). ``write=False`` so the gate
    never mutates the committed ``results.json``.
    """

    baseline = load_baseline(baseline_path)
    report = run_golden(write=False)
    _print_comparison(report, baseline)

    regressions = check_regressions(report, baseline)
    if not regressions:
        print("PASS: no rubric category regressed vs baseline.")
        return 0

    print(f"FAIL: {len(regressions)} rubric category(ies) regressed vs baseline:")
    for reg in regressions:
        print(f"  - {reg.describe()}")
    return 1


def write_baseline(path: Path = BASELINE_PATH) -> EvalReport:
    """Regenerate the committed baseline from a fresh clean run (deliberate)."""

    report = run_golden(write=False)
    existing = json.loads(path.read_text()) if path.exists() else {}
    snapshot = {
        "_comment": existing.get(
            "_comment",
            "Committed baseline snapshot of the golden-set per-category pass "
            "rates (PRP-12, FR-8). Regenerate deliberately with "
            "`python -m copilot.evals.gate --update-baseline`.",
        ),
        "overall_pass_rate": report.overall_pass_rate,
        "pass_threshold": float(existing.get("pass_threshold", DEFAULT_THRESHOLD)),
        "per_category_pass_rate": dict(sorted(report.per_category_pass_rate.items())),
        "regression_tolerance": float(existing.get("regression_tolerance", DEFAULT_TOLERANCE)),
        "total": report.total,
    }
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(f"wrote baseline {path} (overall {report.overall_pass_rate:.4f})")
    return report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--update-baseline" in args:
        write_baseline()
        return 0
    return run_gate()


if __name__ == "__main__":
    raise SystemExit(main())
