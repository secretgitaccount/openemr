"""Behavioral tests for the hybrid RAG retriever (PRP-08, FR-4).

Proves the local BM25 + FAISS → RRF → cross-encoder pipeline:

* **Relevance** — a diabetes query surfaces the ADA metformin chunk in top-k;
  an HTN query surfaces a hypertension chunk.
* **Contract** — results are typed ``GuidelineEvidence`` (chunk + score +
  retriever), and the run is deterministic on the fixed corpus.
* **Rerank is wired** — a crafted lexical-decoy query has a different top-1
  *after* the cross-encoder than pure BM25 gives, so the reranker is doing real
  work, not passing through.
* **Offline** — with all network access severed, a warmed retriever still
  answers (models are cached locally; nothing leaves the box at query time).
* **Observability** — ``retrieve_hit_rate`` derives a corpus-level hit rate.

These tests assume the two model weights are already cached locally (the
one-time HuggingFace download happens on first ever build). No API key, no
patient data.
"""

from __future__ import annotations

import socket

import pytest

from copilot.rag.chunk import GuidelineChunk
from copilot.rag.index import build_index
from copilot.rag.retrieve import (
    GuidelineEvidence,
    bm25_ranking,
    retrieve_evidence,
    retrieve_hit_rate,
)

# The only chunk in the corpus that names metformin — the "ADA metformin chunk".
METFORMIN_CHUNK_ID = "diabetes::ada_standards_diabetes::03"

DIABETES_QUERY = "first-line agent for type 2 diabetes"
HTN_QUERY = "blood pressure target for a treated hypertensive patient"


def _topic_of(chunk: GuidelineChunk) -> str:
    return chunk.chunk_id.split("::", 1)[0]


# --- relevance -------------------------------------------------------------


def test_diabetes_query_returns_metformin_chunk_in_top_k() -> None:
    evidence = retrieve_evidence(DIABETES_QUERY, k=4)
    ids = [e.chunk.chunk_id for e in evidence]
    assert METFORMIN_CHUNK_ID in ids, ids
    # It really is the metformin-bearing ADA chunk.
    (metformin,) = [e.chunk for e in evidence if e.chunk.chunk_id == METFORMIN_CHUNK_ID]
    assert "metformin" in metformin.text.lower()
    assert metformin.source_org.startswith("American Diabetes Association")


def test_hypertension_query_returns_hypertension_chunk_in_top_k() -> None:
    evidence = retrieve_evidence(HTN_QUERY, k=4)
    topics = {_topic_of(e.chunk) for e in evidence}
    assert "hypertension" in topics, [e.chunk.chunk_id for e in evidence]
    assert evidence[0].chunk.chunk_id.startswith("hypertension::")


# --- contract / shape ------------------------------------------------------


def test_returns_typed_guideline_evidence() -> None:
    evidence = retrieve_evidence(DIABETES_QUERY, k=3)
    assert 1 <= len(evidence) <= 3
    for e in evidence:
        assert isinstance(e, GuidelineEvidence)
        assert isinstance(e.chunk, GuidelineChunk)
        assert isinstance(e.score, float)
        assert e.retriever in {"hybrid", "sparse", "dense"}
    # scores are sorted best-first (cross-encoder relevance, higher is better)
    scores = [e.score for e in evidence]
    assert scores == sorted(scores, reverse=True)


def test_k_bounds_result_count() -> None:
    assert len(retrieve_evidence(DIABETES_QUERY, k=1)) == 1
    assert len(retrieve_evidence(DIABETES_QUERY, k=4)) == 4


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_query_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        retrieve_evidence(bad)


def test_non_positive_k_rejected() -> None:
    with pytest.raises(ValueError):
        retrieve_evidence(DIABETES_QUERY, k=0)


# --- determinism -----------------------------------------------------------


def test_deterministic_on_fixed_corpus() -> None:
    first = retrieve_evidence(DIABETES_QUERY, k=4)
    second = retrieve_evidence(DIABETES_QUERY, k=4)
    assert [e.chunk.chunk_id for e in first] == [e.chunk.chunk_id for e in second]
    assert [e.score for e in first] == pytest.approx([e.score for e in second])


def test_build_index_is_cached() -> None:
    # Lazy + cached: same corpus returns the identical built index object.
    assert build_index() is build_index()


# --- reranker actually reorders (not a passthrough) ------------------------


def test_reranker_reorders_over_pure_bm25() -> None:
    # Lexical decoy: "DASH diet" occurs verbatim only in the hypertension
    # lifestyle chunk, so BM25 ranks that first — but the question is about
    # statin *eligibility*. A working cross-encoder demotes the decoy and
    # promotes the USPSTF "who should be offered a statin" chunk.
    query = "other than the DASH diet what makes someone eligible for a statin"

    bm25_top1 = bm25_ranking(query, k=1)[0]
    reranked = retrieve_evidence(query, k=4)
    rerank_top1 = reranked[0].chunk.chunk_id

    assert bm25_top1 == "hypertension::acc_aha_hypertension::03"
    assert rerank_top1 == "lipids::uspstf_lipids::01"
    # The crux: rerank changed the #1 result — proof the reranker is wired.
    assert rerank_top1 != bm25_top1


# --- fully offline at query time -------------------------------------------


def test_offline_at_query_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Warm the model singletons (allowed one-time load), then sever all network
    # and prove a query still resolves entirely on-box (HIPAA posture).
    retrieve_evidence("warm up the models", k=2)

    def _no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted at query time")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)

    evidence = retrieve_evidence(DIABETES_QUERY, k=4)
    assert len(evidence) == 4
    assert any(e.chunk.chunk_id == METFORMIN_CHUNK_ID for e in evidence)


# --- observability: hit rate -----------------------------------------------


def test_retrieve_hit_rate_derivable() -> None:
    cases = [
        (DIABETES_QUERY, {METFORMIN_CHUNK_ID}),
        (HTN_QUERY, {f"hypertension::acc_aha_hypertension::{n:02d}" for n in range(1, 5)}),
    ]
    assert retrieve_hit_rate(cases, k=4) == pytest.approx(1.0)


def test_retrieve_hit_rate_empty_is_zero() -> None:
    assert retrieve_hit_rate([], k=4) == 0.0


def test_retrieve_hit_rate_misses_when_expectation_absent() -> None:
    # An impossible expectation drives the rate to 0 — the metric can distinguish
    # hits from misses (not hard-wired to 1.0).
    assert retrieve_hit_rate([(DIABETES_QUERY, {"no::such::chunk"})], k=4) == 0.0
