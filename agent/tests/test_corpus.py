"""Contract tests for the guideline corpus + chunking (PRP-07).

Proves the evidence corpus loads to enough citable chunks, that every chunk
carries complete source attribution, that ``chunk_id``s are deterministic and
unique across runs, that each demo topic has retrieval signal (>=2 chunks), and
that the corpus is public guidance with no PHI leaking in.
"""

from __future__ import annotations

import re

import pytest

from copilot.rag.chunk import GuidelineChunk, load_corpus

_TOPICS = ("diabetes", "hypertension", "lipids")


def _topic_of(chunk: GuidelineChunk) -> str:
    """Recover a chunk's topic from its ``<topic>::<slug>::<NN>`` id."""
    return chunk.chunk_id.split("::", 1)[0]


def test_corpus_loads_at_least_ten_chunks() -> None:
    chunks = load_corpus()
    assert len(chunks) >= 10, f"expected >=10 chunks, got {len(chunks)}"
    assert all(isinstance(c, GuidelineChunk) for c in chunks)


def test_every_chunk_has_complete_source_metadata() -> None:
    for chunk in load_corpus():
        for field in ("chunk_id", "source_title", "source_org", "citation", "section", "text"):
            value = getattr(chunk, field)
            assert isinstance(value, str) and value.strip(), (
                f"{chunk.chunk_id}: field '{field}' is empty"
            )


def test_chunk_ids_are_unique() -> None:
    ids = [c.chunk_id for c in load_corpus()]
    assert len(ids) == len(set(ids)), "chunk_ids are not unique"


def test_chunk_ids_are_stable_across_calls() -> None:
    first = load_corpus()
    second = load_corpus()
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    # Full content, not just ids, must be reproducible run to run.
    assert first == second


def test_each_topic_has_at_least_two_chunks() -> None:
    chunks = load_corpus()
    for topic in _TOPICS:
        count = sum(1 for c in chunks if _topic_of(c) == topic)
        assert count >= 2, f"topic '{topic}' has only {count} chunk(s), need >=2"


def test_chunks_are_frozen() -> None:
    chunk = load_corpus()[0]
    with pytest.raises(Exception):
        chunk.text = "mutated"  # type: ignore[misc]


# Patterns that would signal real patient data slipped into public guidance.
_PHI_PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "mrn": re.compile(r"\bMRN[:#]?\s*\d+", re.IGNORECASE),
    "phone": re.compile(r"\b\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "dob": re.compile(r"\b(DOB|date of birth)\b", re.IGNORECASE),
}


def test_corpus_contains_no_phi() -> None:
    for chunk in load_corpus():
        for label, pattern in _PHI_PATTERNS.items():
            assert not pattern.search(chunk.text), (
                f"{chunk.chunk_id}: possible {label} PHI in corpus text"
            )
