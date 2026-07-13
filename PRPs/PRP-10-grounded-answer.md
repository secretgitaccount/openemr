# PRP-10 — Grounded answer + citation contract

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-09 · **Blocks:** PRP-11, PRP-13, PRP-14 · **Needs key:** BUILD no (stubbed); LIVE smoke in PRP-14

## Goal (atomic)
Assemble the final grounded answer that **separates patient-record facts from
guideline evidence** (FR-6), where every clinical claim carries the machine-
readable `SourceCitation`. Reuse the Week-1 verification gate to drop any claim
that isn't grounded. Expose the Week-2 flow endpoint.

## Context / files owned
- `agent/src/copilot/graph/answer.py`, `agent/src/copilot/api/w2flow.py`, and the
  **only** Wave-5 edit to `main.py` (mount the w2flow router).
- Consumes PRP-09 `GraphResult`; reuses `verification/gate.py` (Week 1) for the
  grounding drop; citations use PRP-02 `SourceCitation`.

## Contract
- `W2Answer` — `{headline, answer_claims: list[Claim], record_facts:
  list[SourceCitation], guideline_evidence: list[SourceCitation], caveats:
  list[str], handoffs: list[Handoff]}`. `Claim` carries ≥1 citation or is dropped.
- `POST /patients/{patient_id}/ask` — `{question, attachments?}` → runs the graph
  → verified `W2Answer`. Record-facts vs guideline-evidence are distinct fields,
  never merged.

## Validation
```bash
cd agent && . .venv/bin/activate && pytest tests/test_answer.py tests/test_w2flow.py -q && pytest -q
```
- Integration (stubbed LLM/VLM + mocked retrieval): answer separates record vs
  guideline evidence; an **ungrounded** claim is dropped by the gate (test);
  every surfaced claim has a `SourceCitation`; the handoff log is returned.
- A "missing data" question yields a safe, grounded partial answer (not a
  hallucinated value) — the missing-data behavior the eval set checks.
- ruff clean; full suite green; no PHI in logs.

## Builder prompt (backend-dev → qa)
> Implement `graph/answer.py` assembling a `W2Answer` from the PRP-09
> `GraphResult`: separate `record_facts` from `guideline_evidence` as distinct
> fields, attach a PRP-02 `SourceCitation` to every claim, and run each claim
> through the Week-1 verification gate so ungrounded claims are dropped. Add
> `api/w2flow.py::POST /patients/{id}/ask` and mount it in `main.py`. Build +
> test with stubbed LLM/VLM and mocked retrieval: record/guideline separation,
> ungrounded-claim drop, citation-present on every claim, safe missing-data
> behavior, handoff log returned. ruff + full pytest green. Hand to qa.
