"""Tests for the lab-history bound (_bound_labs): latest-per-test + keep abnormals.

Guards the minimum-necessary reduction (NFR-4): a long lab history collapses to
the most recent readings per test plus every abnormal, without dropping
clinically significant old results.
"""

from __future__ import annotations

from datetime import datetime, timezone

from copilot.openemr.retrieval import _bound_labs
from copilot.schemas.clinical import LabResult
from copilot.schemas.core import SourceRef


def _lab(name: str, day: int, *, abnormal: bool | None = None) -> LabResult:
    ts = datetime(2020, 1, day, tzinfo=timezone.utc)
    rid = f"{name}-{day}"
    return LabResult(
        id=rid, name=name, value="1", unit="x", effective=ts, abnormal=abnormal,
        source=SourceRef(resource_type="Observation", id=rid, timestamp=ts),
    )


def test_keeps_latest_per_test_and_drops_older_normals() -> None:
    labs = [_lab("Glucose", d) for d in range(1, 8)] + [_lab("Sodium", 1)]  # 7 glucose, 1 sodium
    kept = _bound_labs(labs, per_test=3)
    names = [lab.name for lab in kept]
    assert names.count("Glucose") == 3
    assert sorted(lab.effective.day for lab in kept if lab.name == "Glucose") == [5, 6, 7]  # newest 3
    assert names.count("Sodium") == 1
    assert len(kept) == 4


def test_abnormal_is_never_dropped_however_old() -> None:
    # 3 recent normal glucose + 1 abnormal glucose from long ago (day 1).
    labs = [_lab("Glucose", d) for d in range(5, 8)] + [_lab("Glucose", 1, abnormal=True)]
    kept = _bound_labs(labs, per_test=3)
    days = {lab.effective.day for lab in kept}
    assert 1 in days, "the old abnormal glucose must be retained"
    assert {5, 6, 7} <= days
    assert len({lab.id for lab in kept}) == len(kept)  # no duplicates
