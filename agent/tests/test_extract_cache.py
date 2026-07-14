"""Unit tests for the in-memory chart-document extraction cache (PRP-17).

No key, no network, no live stack: the chart reader and the VLM extractor are
duck-typed stubs, so these exercise the cache + the extract-only read path with
neither an Anthropic call nor an OpenEMR call.

Coverage (the PRP's validation gates):

* the cache is a bounded LRU keyed on ``(patient_id, document_id)`` (no PHI);
* the doctor's **read** (:func:`ingest_chart_document`) warms the cache and the
  subsequent **ask** (:func:`extract_chart_document`) reuses it — the extractor
  runs **once** across read + ask (asserted by call count), and the ask does not
  even re-fetch the bytes;
* a cold ask extracts exactly once and warms the cache;
* ``extract_chart_document`` never persists and grounds citations to the doc id;
* an unknown ``doc_type`` raises before any work.
"""

from __future__ import annotations

import pytest

from copilot.documents.chart_read import extract_chart_document, ingest_chart_document
from copilot.documents.extract_cache import ExtractionCache, cache_key, extract_cache
from copilot.documents.ingest import IngestError
from copilot.documents.schemas import (
    CitedList,
    IntakeDemographics,
    IntakeFacts,
    SourceCitation,
)

PID = "pat-uuid-1"
DOC_ID = "1849"


# ---------------------------------------------------------------------------
# Fixtures: a validated IntakeFacts (no derived-write path -> no writer needed)
# and duck-typed reader / extractor stubs that count how often they were used.
# ---------------------------------------------------------------------------


def _cit(source_id: str = "raw-tmp-name") -> SourceCitation:
    return SourceCitation(
        source_type="intake_form",
        source_id=source_id,
        page_or_section="page 1",
        quote_or_value="none reported",
    )


def _intake() -> IntakeFacts:
    return IntakeFacts(
        demographics=IntakeDemographics(name=None, dob=None, sex=None, citation=_cit()),
        chief_concern=None,
        current_medications=CitedList(items=[], citation=_cit()),
        allergies=CitedList(items=[], citation=_cit()),
        family_history=CitedList(items=[], citation=_cit()),
        extraction_confidence=0.8,
        source=_cit(),
    )


class _FakeReader:
    """A chart reader stub that returns fixed bytes and counts fetches."""

    def __init__(self, data: bytes = b"%PDF-1.4 fake") -> None:
        self._data = data
        self.fetch_calls = 0

    async def fetch_document_bytes(self, patient_id: str, doc_id: str) -> bytes:
        self.fetch_calls += 1
        return self._data


class _CountingExtractor:
    """A VLM stand-in that returns a fixed extraction and counts invocations."""

    def __init__(self, result: IntakeFacts) -> None:
        self._result = result
        self.calls = 0

    async def extract_intake(self, file_path: object) -> IntakeFacts:
        self.calls += 1
        return self._result

    async def extract_lab(self, file_path: object) -> IntakeFacts:  # pragma: no cover
        self.calls += 1
        return self._result


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    extract_cache.clear()


# ---------------------------------------------------------------------------
# Cache: LRU + key shape
# ---------------------------------------------------------------------------


def test_cache_key_is_patient_and_document_only() -> None:
    # No PHI (no clinical values) in the key — just the two stable identifiers.
    assert cache_key(PID, DOC_ID) == (PID, DOC_ID)
    assert cache_key(PID, DOC_ID) != cache_key(PID, "other")
    assert cache_key(PID, DOC_ID) != cache_key("other", DOC_ID)


def test_cache_get_put_roundtrip() -> None:
    c = ExtractionCache()
    facts = _intake()
    c.put(PID, DOC_ID, facts)
    assert c.get(PID, DOC_ID) is facts
    assert c.get(PID, "missing") is None


def test_cache_evicts_least_recently_used() -> None:
    c = ExtractionCache(maxsize=2)
    c.put(PID, "a", _intake())
    c.put(PID, "b", _intake())
    c.get(PID, "a")  # touch 'a' so 'b' is now LRU
    c.put(PID, "c", _intake())  # exceeds maxsize -> evict 'b'
    assert c.get(PID, "a") is not None
    assert c.get(PID, "b") is None
    assert c.get(PID, "c") is not None
    assert len(c) == 2


# ---------------------------------------------------------------------------
# Read warms it, Ask reuses it — extractor runs ONCE across read + ask
# ---------------------------------------------------------------------------


async def test_read_warms_cache_and_ask_reuses_it_no_second_vlm() -> None:
    reader = _FakeReader()
    extractor = _CountingExtractor(_intake())

    # 1) The doctor READS the chart document (fetch + extract + populate cache).
    await ingest_chart_document(
        PID, DOC_ID, "intake_form", reader=reader, extractor=extractor
    )
    assert extractor.calls == 1
    assert reader.fetch_calls == 1
    assert extract_cache.get(PID, DOC_ID) is not None  # cache warmed

    # 2) The subsequent ASK grounds on the same document -> cache hit.
    facts = await extract_chart_document(
        PID, DOC_ID, "intake_form", reader=reader, extractor=extractor
    )

    # The VLM ran exactly ONCE across read + ask, and the ask did not re-fetch.
    assert extractor.calls == 1
    assert reader.fetch_calls == 1
    # Citations are grounded to the stable OpenEMR document id.
    assert facts.source.source_id == DOC_ID
    assert facts.demographics.citation.source_id == DOC_ID


async def test_ask_on_cold_cache_extracts_once_then_warms() -> None:
    reader = _FakeReader()
    extractor = _CountingExtractor(_intake())

    facts = await extract_chart_document(
        PID, DOC_ID, "intake_form", reader=reader, extractor=extractor
    )
    assert extractor.calls == 1
    assert facts.source.source_id == DOC_ID
    assert extract_cache.get(PID, DOC_ID) is facts

    # A second ask now hits the warm cache: no further VLM / fetch.
    again = await extract_chart_document(
        PID, DOC_ID, "intake_form", reader=reader, extractor=extractor
    )
    assert again is facts
    assert extractor.calls == 1
    assert reader.fetch_calls == 1


async def test_extract_chart_document_unknown_doc_type_raises() -> None:
    with pytest.raises(IngestError):
        await extract_chart_document(PID, DOC_ID, "radiology_dicom", reader=_FakeReader())
