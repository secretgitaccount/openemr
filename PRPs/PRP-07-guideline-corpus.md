# PRP-07 — Guideline corpus + chunking

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-01 · **Blocks:** PRP-08 · **Needs key:** no

## Goal (atomic)
Assemble a small, **citable** clinical-guideline corpus matched to the demo
panel (diabetes / hypertension / lipids) and chunk it into retrievable units
with stable ids and full source attribution. This is the evidence half of the
answer — kept strictly separate from patient-record facts.

## Context / files owned
- `agent/src/copilot/rag/corpus/**` (markdown), `agent/src/copilot/rag/chunk.py`.
- Sources: ADA Standards of Care, ACC/AHA hypertension, USPSTF. **Paraphrase /
  summarize** the guidance in our own words with a citation — do not paste large
  verbatim copyrighted text. Short attributed quotes only.

## Contract
- Each corpus file carries front-matter: `{source_title, source_org, citation,
  url, topic}`.
- `GuidelineChunk` (Pydantic, frozen): `{chunk_id, source_title, source_org,
  citation, section, text}`. `chunk_id` is stable + deterministic.
- `load_corpus() -> list[GuidelineChunk]`.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_corpus.py -q
```
- Corpus loads to ≥10 chunks; every chunk has complete source metadata (test).
- `chunk_id`s are stable across runs (deterministic) and unique (test).
- No PHI, no patient data — public guidance only.
- Each topic (diabetes, HTN, lipids) has ≥2 chunks so retrieval has signal.

## Builder prompt (backend-dev → qa)
> Build a small guideline corpus under `rag/corpus/` (markdown with source
> front-matter) covering diabetes, hypertension, and lipids, drawn from ADA
> Standards of Care, ACC/AHA, and USPSTF. **Paraphrase in our own words with a
> citation — no large verbatim copyrighted text.** Implement `rag/chunk.py` with
> a frozen `GuidelineChunk` model and `load_corpus()` yielding ≥10 chunks, each
> with stable/unique `chunk_id` and complete source metadata. Test:
> completeness, determinism/uniqueness of ids, ≥2 chunks per topic, no PHI. ruff
> + pytest green. Hand to qa.
