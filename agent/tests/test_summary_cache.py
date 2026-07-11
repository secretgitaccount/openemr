"""Unit tests for the in-memory generated-summary cache."""

from __future__ import annotations

from datetime import UTC, datetime

from copilot.orchestrator.summary_cache import (
    CachedSummary,
    SummaryCache,
    cache_key,
    content_hash,
)
from copilot.schemas.clinical import CriticalSet, Problem
from copilot.schemas.core import SourceRef


def _problem(name: str = "Diabetes") -> Problem:
    return Problem(id="c1", name=name, source=SourceRef(resource_type="Condition", id="c1"))


# --- content hash -----------------------------------------------------------


def test_hash_ignores_retrieved_at():
    # retrieved_at changes every fetch; it must NOT change the hash or the cache
    # would never hit.
    a = CriticalSet(retrieved_at=datetime(2026, 1, 1, tzinfo=UTC))
    b = CriticalSet(retrieved_at=datetime(2026, 7, 1, tzinfo=UTC))
    assert content_hash(a, None) == content_hash(b, None)


def test_hash_changes_when_chart_data_changes():
    empty = CriticalSet()
    with_problem = CriticalSet(problems=[_problem()])
    assert content_hash(empty, None) != content_hash(with_problem, None)


def test_hash_changes_when_a_problem_changes():
    a = CriticalSet(problems=[_problem("Diabetes")])
    b = CriticalSet(problems=[_problem("Prediabetes")])
    assert content_hash(a, None) != content_hash(b, None)


def test_cache_key_includes_version_and_patient():
    cs = CriticalSet()
    k1 = cache_key("pat-1", cs, None, "sonnet:v1")
    k2 = cache_key("pat-1", cs, None, "sonnet:v2")  # version bump
    k3 = cache_key("pat-2", cs, None, "sonnet:v1")  # different patient
    assert k1 != k2 and k1 != k3
    assert k1.startswith("pat-1|sonnet:v1|")


# --- LRU cache --------------------------------------------------------------


def _entry() -> CachedSummary:
    return CachedSummary(verified=object(), generated_at=datetime.now(UTC))  # type: ignore[arg-type]


def test_cache_get_put_roundtrip():
    c = SummaryCache()
    e = _entry()
    c.put("k", e)
    assert c.get("k") is e
    assert c.get("missing") is None


def test_cache_evicts_least_recently_used():
    c = SummaryCache(maxsize=2)
    c.put("a", _entry())
    c.put("b", _entry())
    c.get("a")            # touch 'a' so 'b' is now LRU
    c.put("c", _entry())  # exceeds maxsize -> evict 'b'
    assert c.get("a") is not None
    assert c.get("b") is None
    assert c.get("c") is not None
    assert len(c) == 2
