"""Hybrid retriever: BM25 + FAISS candidates → RRF fusion → cross-encoder rerank.

The public entry point is :func:`retrieve_evidence`, which returns the top-``k``
grounded :class:`GuidelineEvidence` for a query. The pipeline is:

1. **Sparse** BM25 and **dense** FAISS each rank the whole corpus.
2. **Reciprocal-rank fusion (RRF)** merges the two rankings into one candidate
   pool — robust to the two scorers being on different scales.
3. A local ``cross-encoder/ms-marco-MiniLM-L-6-v2`` **reranks** the pool by
   scoring each ``(query, passage)`` pair jointly, and the top ``k`` are returned.

Fully local (HIPAA posture): the cross-encoder is a process-wide singleton whose
weights are cached by ``sentence-transformers``. The first call downloads them
once; every call after that — and all of query time — is offline. Model load is
timeout-guarded via :func:`copilot.rag.index.run_with_timeout`.

Observability: :func:`retrieve_hit_rate` derives a corpus-level hit rate from
labelled cases, and :func:`bm25_ranking` exposes the pre-rerank lexical order so
callers (and tests) can see how much the reranker moved things.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from copilot.logging import get_logger
from copilot.rag.chunk import GuidelineChunk
from copilot.rag.index import (
    HybridIndex,
    _tokenize,  # shared corpus/query tokenizer
    build_index,
    embed_query,
    run_with_timeout,
)

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

__all__ = [
    "GuidelineEvidence",
    "retrieve_evidence",
    "bm25_ranking",
    "retrieve_hit_rate",
    "RERANKER_MODEL_NAME",
]

logger = get_logger(__name__)

#: Local cross-encoder reranker; ~90 MB, cached after first download.
RERANKER_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

#: RRF damping constant. 60 is the canonical value from the original TREC paper —
#: large enough that no single top rank dominates the fused score.
_RRF_K = 60

#: How many fused candidates to hand the (more expensive) cross-encoder.
_RERANK_POOL = 8

#: A retriever is credited with "surfacing" a chunk if it ranked it this high;
#: used only to label ``GuidelineEvidence.retriever`` for observability.
_SURFACE_DEPTH = 5

_MODEL_LOAD_TIMEOUT_SECONDS = float(os.getenv("COPILOT_RAG_MODEL_TIMEOUT", "600"))


class GuidelineEvidence(BaseModel):
    """One reranked, grounded piece of evidence returned to the caller (FR-4).

    Carries the citable :class:`GuidelineChunk`, the reranker's relevance
    ``score`` (higher is more relevant), and which retriever surfaced it
    (``"hybrid"`` / ``"sparse"`` / ``"dense"``) for observability.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk: GuidelineChunk
    score: float = Field(description="Cross-encoder relevance score; higher is better.")
    retriever: str = Field(description="Which retriever surfaced this chunk.")


# --- cross-encoder singleton ----------------------------------------------

_reranker: CrossEncoder | None = None


def _get_reranker() -> CrossEncoder:
    """Load ``RERANKER_MODEL_NAME`` once (module singleton), timeout-guarded."""
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        logger.info("rag_reranker_load_start", model=RERANKER_MODEL_NAME)
        _reranker = run_with_timeout(
            lambda: CrossEncoder(RERANKER_MODEL_NAME),
            _MODEL_LOAD_TIMEOUT_SECONDS,
            what=f"reranker load ({RERANKER_MODEL_NAME})",
        )
        logger.info("rag_reranker_load_done", model=RERANKER_MODEL_NAME)
    return _reranker


# --- candidate generation --------------------------------------------------


def _bm25_scores(index: HybridIndex, query: str) -> np.ndarray:
    return np.asarray(index.bm25.get_scores(_tokenize(query)), dtype=float)


def _dense_scores(index: HybridIndex, query: str) -> np.ndarray:
    """Cosine similarity of the query against every chunk, in positional order."""
    query_vec = embed_query(query)
    n = len(index)
    scores, positions = index.faiss_index.search(query_vec, n)
    ordered = np.zeros(n, dtype=float)
    ordered[positions[0]] = scores[0]
    return ordered


def _ranked_positions(scores: np.ndarray) -> list[int]:
    """Chunk positions ordered by descending score (stable on ties)."""
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))


def _reciprocal_rank_fusion(rankings: Sequence[Sequence[int]]) -> dict[int, float]:
    """Fuse multiple rankings into one ``position -> fused score`` mapping."""
    fused: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, position in enumerate(ranking):
            fused[position] += 1.0 / (_RRF_K + rank + 1)
    return fused


def _retriever_label(position: int, bm25_top: set[int], dense_top: set[int]) -> str:
    in_bm25 = position in bm25_top
    in_dense = position in dense_top
    if in_bm25 and in_dense:
        return "hybrid"
    if in_bm25:
        return "sparse"
    if in_dense:
        return "dense"
    return "hybrid"


# --- public API ------------------------------------------------------------


def retrieve_evidence(query: str, k: int = 4) -> list[GuidelineEvidence]:
    """Return the top-``k`` grounded evidence for ``query`` (BM25+FAISS → rerank).

    Deterministic given the fixed corpus and cached models. Runs fully offline
    after the one-time model download.
    """
    if not query or not query.strip():
        raise ValueError("retrieve_evidence: query must be non-empty")
    if k <= 0:
        raise ValueError("retrieve_evidence: k must be positive")

    index = build_index()

    bm25_ranked = _ranked_positions(_bm25_scores(index, query))
    dense_ranked = _ranked_positions(_dense_scores(index, query))

    fused = _reciprocal_rank_fusion([bm25_ranked, dense_ranked])
    pool = sorted(fused, key=lambda pos: (-fused[pos], pos))[: min(_RERANK_POOL, len(index))]

    reranker = _get_reranker()
    pairs = [(query, index.chunks[pos].text) for pos in pool]
    rerank_scores = run_with_timeout(
        lambda: reranker.predict(pairs),
        _MODEL_LOAD_TIMEOUT_SECONDS,
        what="cross-encoder predict",
    )

    scored = sorted(
        zip(pool, (float(s) for s in rerank_scores), strict=True),
        key=lambda ps: (-ps[1], ps[0]),
    )

    bm25_top = set(bm25_ranked[:_SURFACE_DEPTH])
    dense_top = set(dense_ranked[:_SURFACE_DEPTH])
    return [
        GuidelineEvidence(
            chunk=index.chunks[pos],
            score=score,
            retriever=_retriever_label(pos, bm25_top, dense_top),
        )
        for pos, score in scored[:k]
    ]


def bm25_ranking(query: str, k: int | None = None) -> list[str]:
    """Chunk ids ordered by pure BM25 (pre-fusion, pre-rerank), for inspection."""
    index = build_index()
    ranked = _ranked_positions(_bm25_scores(index, query))
    if k is not None:
        ranked = ranked[:k]
    return [index.chunks[pos].chunk_id for pos in ranked]


def retrieve_hit_rate(
    cases: Iterable[tuple[str, Iterable[str]]],
    k: int = 4,
) -> float:
    """Fraction of ``(query, relevant_chunk_ids)`` cases whose top-``k`` hits one.

    The observability primitive behind a ``retrieve_hit_rate`` metric (PRP-14):
    a case is a hit if any of its relevant chunk ids appears in the top-``k``
    retrieved evidence. Returns 0.0 for an empty case set.
    """
    cases = list(cases)
    if not cases:
        return 0.0
    hits = 0
    for query, relevant in cases:
        relevant_ids = set(relevant)
        retrieved = {ev.chunk.chunk_id for ev in retrieve_evidence(query, k=k)}
        if retrieved & relevant_ids:
            hits += 1
    return hits / len(cases)
