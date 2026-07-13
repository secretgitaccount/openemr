"""Corpus indexing: FAISS dense vectors + BM25 lexical index (PRP-08).

Builds the two halves of the hybrid retriever over PRP-07's
:class:`~copilot.rag.chunk.GuidelineChunk` corpus:

* a **sparse** BM25 index (``rank_bm25``) for exact lexical overlap, and
* a **dense** FAISS index of normalized embeddings from the local
  ``BAAI/bge-small-en-v1.5`` model (cosine similarity via inner product).

Everything runs on-box (HIPAA posture): the embedding model is a process-wide
singleton loaded once, its weights cached locally by ``sentence-transformers``.
The **first** import-and-build downloads the weights from HuggingFace (one time);
after that, and at query time, nothing leaves the machine. Model load and the
first embedding call are wrapped in a timeout guard so a hung download can never
silently block the caller forever.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING, TypeVar

import faiss
import numpy as np
from rank_bm25 import BM25Okapi

from copilot.logging import get_logger
from copilot.rag.chunk import GuidelineChunk, load_corpus

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

__all__ = [
    "HybridIndex",
    "build_index",
    "embed_query",
    "run_with_timeout",
    "EMBEDDING_MODEL_NAME",
]

logger = get_logger(__name__)

#: Local dense-embedding model. Small, fast, strong on retrieval; ~130 MB.
EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"

#: bge models expect this instruction prefixed on *queries* (not passages) so the
#: query and passage embeddings land in the same retrieval space.
_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

#: Generous ceiling: the first call may download weights; later loads are local
#: and near-instant. Overridable for constrained CI via env.
_MODEL_LOAD_TIMEOUT_SECONDS = float(os.getenv("COPILOT_RAG_MODEL_TIMEOUT", "600"))

_TOKEN_RE = re.compile(r"[a-z0-9]+")

T = TypeVar("T")


def run_with_timeout(fn: Callable[[], T], timeout: float, *, what: str) -> T:
    """Run ``fn`` on a worker thread, raising :class:`TimeoutError` if it stalls.

    Used to guard model load / first-call so a hung network fetch surfaces as a
    clear error instead of an indefinite block. On timeout the worker thread is
    abandoned (it cannot be force-killed), which is acceptable for a one-shot
    load guard.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError as exc:  # pragma: no cover - timing dependent
            raise TimeoutError(f"{what} exceeded {timeout:.0f}s timeout") from exc


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokenization shared by corpus and query."""
    return _TOKEN_RE.findall(text.lower())


def _indexable_text(chunk: GuidelineChunk) -> str:
    """Text used for both lexical and dense indexing: heading + body."""
    return f"{chunk.section}\n{chunk.text}"


# --- embedding model singleton --------------------------------------------

_embedder: SentenceTransformer | None = None


def _get_embedder() -> SentenceTransformer:
    """Load ``EMBEDDING_MODEL_NAME`` once (module singleton), timeout-guarded."""
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        logger.info("rag_embedder_load_start", model=EMBEDDING_MODEL_NAME)
        _embedder = run_with_timeout(
            lambda: SentenceTransformer(EMBEDDING_MODEL_NAME),
            _MODEL_LOAD_TIMEOUT_SECONDS,
            what=f"embedding model load ({EMBEDDING_MODEL_NAME})",
        )
        logger.info("rag_embedder_load_done", model=EMBEDDING_MODEL_NAME)
    return _embedder


def _encode(texts: Sequence[str]) -> np.ndarray:
    """Encode passages to L2-normalized float32 vectors (cosine via dot)."""
    model = _get_embedder()
    vectors = run_with_timeout(
        lambda: model.encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ),
        _MODEL_LOAD_TIMEOUT_SECONDS,
        what="embedding encode",
    )
    return np.asarray(vectors, dtype=np.float32)


def embed_query(query: str) -> np.ndarray:
    """Embed a search query (with the bge instruction) as a normalized vector."""
    return _encode([_QUERY_INSTRUCTION + query])


# --- the hybrid index ------------------------------------------------------


class HybridIndex:
    """A built BM25 + FAISS index over a fixed list of guideline chunks.

    Immutable after construction. Holds the chunk list in stable order; both the
    BM25 index and the FAISS index are aligned to that order by positional index.
    """

    __slots__ = ("chunks", "bm25", "faiss_index", "embedding_dim")

    def __init__(
        self,
        chunks: tuple[GuidelineChunk, ...],
        bm25: BM25Okapi,
        faiss_index: faiss.Index,
        embedding_dim: int,
    ) -> None:
        self.chunks = chunks
        self.bm25 = bm25
        self.faiss_index = faiss_index
        self.embedding_dim = embedding_dim

    def __len__(self) -> int:
        return len(self.chunks)


#: Cache keyed by the ordered chunk-id tuple, so a rebuild over the same corpus
#: reuses the (expensive) embeddings and FAISS index.
_INDEX_CACHE: dict[tuple[str, ...], HybridIndex] = {}


def build_index(chunks: Sequence[GuidelineChunk] | None = None) -> HybridIndex:
    """Build (or return a cached) hybrid index over ``chunks``.

    ``chunks`` defaults to the full PRP-07 corpus via
    :func:`~copilot.rag.chunk.load_corpus`. Lazy and cached: the first build for
    a given corpus embeds every chunk and constructs the FAISS index; subsequent
    calls with the same chunk ids return the cached instance.
    """
    resolved = tuple(chunks) if chunks is not None else tuple(load_corpus())
    if not resolved:
        raise ValueError("build_index: cannot index an empty corpus")

    key = tuple(c.chunk_id for c in resolved)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    logger.info("rag_index_build_start", n_chunks=len(resolved))

    texts = [_indexable_text(c) for c in resolved]
    bm25 = BM25Okapi([_tokenize(t) for t in texts])

    embeddings = _encode(texts)
    embedding_dim = int(embeddings.shape[1])
    faiss_index = faiss.IndexFlatIP(embedding_dim)
    faiss_index.add(embeddings)

    index = HybridIndex(resolved, bm25, faiss_index, embedding_dim)
    _INDEX_CACHE[key] = index
    logger.info("rag_index_build_done", n_chunks=len(resolved), dim=embedding_dim)
    return index
