# PRP-01 — Dependencies + package scaffold

**Role:** backend-dev · **QA:** qa · **Depends on:** — · **Blocks:** PRP-02, PRP-03, PRP-07 · **Needs key:** no

## Goal (atomic)
Add the Week 2 dependencies and create empty, importable package skeletons so
downstream PRPs have a home. Nothing functional yet — just the wiring compiles.

## Context / files
- `agent/requirements.txt` — append the Week 2 block.
- Create packages under `agent/src/copilot/`:
  `documents/` (`__init__.py`, `schemas.py`, `extract.py`, `ingest.py`,
  `openemr_write.py`), `rag/` (`__init__.py`, `corpus/`, `index.py`,
  `retrieve.py`), `graph/` (`__init__.py`, `state.py`, `supervisor.py`,
  `workers.py`).
- `agent/tests/` — mirror with empty test modules where sensible.

## Deps to add (pin conservative floors)
```
langgraph>=0.2
faiss-cpu>=1.8
rank-bm25>=0.2
sentence-transformers>=3.0    # pulls torch — CPU wheels
pdfplumber>=0.11
pypdf>=5.0
reportlab>=4.2                # dev/test asset generation
```

## Validation gates
- [ ] `pip install -r agent/requirements.txt` succeeds in `agent/.venv`.
- [ ] `python -c "import langgraph, faiss, rank_bm25, sentence_transformers, pdfplumber, reportlab"` OK.
- [ ] `python -c "import copilot.documents, copilot.rag, copilot.graph"` OK.
- [ ] `ruff check` clean; existing `pytest` suite still green (no regressions).
- [ ] Note the installed torch size + wheel variant in the PR description.

## Agent launch prompt
> Add the Week 2 dependency block to `agent/requirements.txt` (langgraph,
> faiss-cpu, rank-bm25, sentence-transformers, pdfplumber, pypdf, reportlab) and
> install into `agent/.venv`. Create importable empty package skeletons for
> `copilot.documents` (schemas, extract, ingest, openemr_write), `copilot.rag`
> (index, retrieve, corpus/), and `copilot.graph` (state, supervisor, workers),
> each with docstrings but no logic. Confirm all imports resolve, ruff is clean,
> and the existing pytest suite still passes. Report the torch install size.
