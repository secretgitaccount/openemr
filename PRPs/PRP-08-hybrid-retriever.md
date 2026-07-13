# PRP-08 — Hybrid RAG retriever + tests

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-07 · **Blocks:** PRP-09 · **Needs key:** no (models are local)

## Goal (atomic)
A reliable **local** hybrid retriever: BM25 (sparse) + FAISS (dense) candidate
generation over the corpus, then a local cross-encoder rerank, returning only
the top grounded evidence with source metadata (FR-4). Nothing leaves the box.

## Context / files owned
- `agent/src/copilot/rag/index.py`, `agent/src/copilot/rag/retrieve.py`,
  `agent/tests/test_rag.py`.
- Models: embeddings `bge-small-en-v1.5`, reranker `ms-marco-MiniLM-L-6-v2`
  (sentence-transformers). Load once (module singleton); cache weights.

## Contract
- `build_index(chunks) -> Index` — BM25 + FAISS over PRP-07 chunks; lazy/cached.
- `retrieve_evidence(query, k=4) -> list[GuidelineEvidence]` where
  `GuidelineEvidence = {chunk: GuidelineChunk, score: float, retriever: str}`.
- Hybrid merge (e.g. reciprocal-rank fusion) → cross-encoder rerank → top-k.
- Outbound-free but wrap model load/first-call with a timeout guard.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_rag.py -q
```
- **Relevance:** a known query ("first-line agent for type 2 diabetes") returns
  the ADA metformin chunk in top-k (test); an HTN query returns an HTN chunk.
- Deterministic given the fixed corpus (test); runs fully offline (no network).
- Reranker actually reorders (top-1 after rerank differs from pure BM25 on a
  crafted case) — proves rerank is wired, not a passthrough.
- `retrieve_hit_rate` is derivable for observability (PRP-14).

## Builder prompt (backend-dev → qa)
> Implement `rag/index.py` (BM25 + FAISS over PRP-07 chunks, lazy-built/cached)
> and `rag/retrieve.py::retrieve_evidence(query, k=4)` returning
> `GuidelineEvidence` (chunk + score + retriever). Fuse sparse+dense candidates
> (reciprocal-rank fusion) then rerank with a local `ms-marco-MiniLM` cross-
> encoder; embeddings via `bge-small-en-v1.5`. Load models once; run fully
> offline. Guard model load/first call with a timeout. Test relevance on known
> diabetes/HTN queries, determinism, offline operation, and that the reranker
> reorders results. ruff + pytest green. Hand to qa.
