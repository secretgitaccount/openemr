"""Unit tests for the background, data-only schedule prewarm (PRP M2-4, FR-13).

The schedule lookup and the per-patient critical-set retrieval are stubbed at the
:mod:`copilot.orchestrator.prewarm` module boundary (no FHIR, no network), and the
cache is the in-memory :class:`TTLCache`. No Anthropic API key is required — and,
by design, prewarm never calls the LLM at all.

Coverage:

* :func:`prewarm_schedule` warms the cache for every scheduled patient (one entry
  per patient id, de-duplicated), reporting ``scheduled``/``warmed`` counts;
* :func:`cached_critical_set` returns the prewarmed set on a hit **without** a
  second FHIR fetch;
* one patient's retrieval failing is recorded in :attr:`PrewarmReport.failed` and
  does not sink the batch (the others still warm);
* a cold :func:`cached_critical_set` fetches live once, then caches so the next
  read is a hit.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copilot.orchestrator import prewarm as prewarm_mod
from copilot.orchestrator.cache import TTLCache
from copilot.orchestrator.prewarm import (
    PrewarmReport,
    cached_critical_set,
    critical_set_key,
    prewarm_schedule,
)
from copilot.schemas.clinical import CriticalSet, Medication, ScheduledPatient
from copilot.schemas.core import SourceRef, ToolResult

PROVIDER = "prov-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubClient:
    """A stand-in for :class:`FhirClient` — the stubbed retrievals ignore it."""


def _scheduled(patient_id: str) -> ScheduledPatient:
    return ScheduledPatient(
        patient_id=patient_id,
        name=f"Patient {patient_id}",
        start=datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
        appointment_id=f"appt-{patient_id}",
        source=SourceRef(resource_type="Appointment", id=f"appt-{patient_id}"),
    )


def _critical_set(patient_id: str) -> CriticalSet:
    return CriticalSet(
        medications=[
            Medication(
                id=f"med-{patient_id}",
                name="Lisinopril",
                status="active",
                source=SourceRef(resource_type="MedicationRequest", id=f"med-{patient_id}"),
            )
        ]
    )


def _patch_schedule(monkeypatch: pytest.MonkeyPatch, patient_ids: list[str]) -> None:
    async def _fake_schedule(provider_id: str, *, client: object) -> ToolResult:
        assert provider_id == PROVIDER
        return ToolResult(data=[_scheduled(pid) for pid in patient_ids])

    monkeypatch.setattr(prewarm_mod, "get_todays_schedule", _fake_schedule)


def _patch_retrieval(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: set[str] | None = None,
) -> dict[str, int]:
    """Stub ``get_critical_set``; return a per-patient call counter.

    Patient ids in ``fail`` raise, exercising the "recorded, not fatal" path.
    """

    fail = fail or set()
    calls: dict[str, int] = {}

    async def _fake_get(patient_id: str, *, client: object) -> CriticalSet:
        calls[patient_id] = calls.get(patient_id, 0) + 1
        if patient_id in fail:
            raise RuntimeError("retrieval blew up")
        return _critical_set(patient_id)

    monkeypatch.setattr(prewarm_mod, "get_critical_set", _fake_get)
    return calls


# ---------------------------------------------------------------------------
# prewarm_schedule populates the cache for every scheduled patient
# ---------------------------------------------------------------------------


async def test_prewarm_populates_cache_for_scheduled_patients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient_ids = ["pat-1", "pat-2", "pat-3"]
    _patch_schedule(monkeypatch, patient_ids)
    calls = _patch_retrieval(monkeypatch)

    cache: TTLCache[CriticalSet] = TTLCache()
    report = await prewarm_schedule(PROVIDER, client=_StubClient(), cache=cache)

    assert isinstance(report, PrewarmReport)
    assert report.scheduled == 3
    assert report.warmed == 3
    assert report.failed == []
    assert report.elapsed_s >= 0.0

    # Each patient's critical set is now warm in the cache under the stable key.
    for pid in patient_ids:
        cached = await cache.get(critical_set_key(pid))
        assert isinstance(cached, CriticalSet)
        assert cached.medications[0].id == f"med-{pid}"

    # Exactly one retrieval per patient.
    assert calls == {"pat-1": 1, "pat-2": 1, "pat-3": 1}


async def test_prewarm_deduplicates_repeat_appointments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A patient with two appointments today is warmed once, not twice.
    _patch_schedule(monkeypatch, ["pat-1", "pat-1", "pat-2"])
    calls = _patch_retrieval(monkeypatch)

    cache: TTLCache[CriticalSet] = TTLCache()
    report = await prewarm_schedule(PROVIDER, client=_StubClient(), cache=cache)

    assert report.scheduled == 2
    assert report.warmed == 2
    assert calls == {"pat-1": 1, "pat-2": 1}


# ---------------------------------------------------------------------------
# cached_critical_set: warm hit does not re-fetch
# ---------------------------------------------------------------------------


async def test_cached_critical_set_hit_skips_second_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_schedule(monkeypatch, ["pat-1"])
    calls = _patch_retrieval(monkeypatch)

    cache: TTLCache[CriticalSet] = TTLCache()
    await prewarm_schedule(PROVIDER, client=_StubClient(), cache=cache)
    assert calls == {"pat-1": 1}  # warmed by prewarm

    # The request path reads the prewarmed set — no second FHIR retrieval.
    got = await cached_critical_set("pat-1", client=_StubClient(), cache=cache)
    assert got.medications[0].id == "med-pat-1"
    assert calls == {"pat-1": 1}  # unchanged: served from cache


# ---------------------------------------------------------------------------
# A single patient failing is recorded, not fatal
# ---------------------------------------------------------------------------


async def test_one_patient_failure_recorded_and_batch_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_schedule(monkeypatch, ["pat-1", "pat-2", "pat-3"])
    _patch_retrieval(monkeypatch, fail={"pat-2"})

    cache: TTLCache[CriticalSet] = TTLCache()
    report = await prewarm_schedule(PROVIDER, client=_StubClient(), cache=cache)

    assert report.scheduled == 3
    assert report.warmed == 2
    assert report.failed == ["pat-2"]

    # The healthy patients still warmed; the failed one is simply absent.
    assert await cache.get(critical_set_key("pat-1")) is not None
    assert await cache.get(critical_set_key("pat-3")) is not None
    assert await cache.get(critical_set_key("pat-2")) is None


# ---------------------------------------------------------------------------
# Cold cached_critical_set fetches live, then caches
# ---------------------------------------------------------------------------


async def test_cold_cached_critical_set_fetches_then_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_retrieval(monkeypatch)  # no schedule needed for a cold read

    cache: TTLCache[CriticalSet] = TTLCache()
    assert await cache.get(critical_set_key("pat-9")) is None  # cold

    got = await cached_critical_set("pat-9", client=_StubClient(), cache=cache)
    assert got.medications[0].id == "med-pat-9"
    assert calls == {"pat-9": 1}  # one live fetch

    # It is now cached: a second read is a hit with no further retrieval.
    again = await cached_critical_set("pat-9", client=_StubClient(), cache=cache)
    assert again.medications[0].id == "med-pat-9"
    assert calls == {"pat-9": 1}


# ---------------------------------------------------------------------------
# Empty schedule: nothing to warm, still a clean report
# ---------------------------------------------------------------------------


async def test_empty_schedule_yields_empty_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_schedule(monkeypatch, [])
    calls = _patch_retrieval(monkeypatch)

    cache: TTLCache[CriticalSet] = TTLCache()
    report = await prewarm_schedule(PROVIDER, client=_StubClient(), cache=cache)

    assert report.scheduled == 0
    assert report.warmed == 0
    assert report.failed == []
    assert calls == {}
