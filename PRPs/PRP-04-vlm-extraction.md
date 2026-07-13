# PRP-04 — VLM extraction + stub test

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-02, PRP-03 · **Blocks:** PRP-06 · **Needs key:** BUILD no (stubbed VLM); LIVE smoke in PRP-06

## Goal (atomic)
Turn a document into a validated `LabReport` / `IntakeFacts` via Claude vision —
where **the schema, not the model, is the source of truth**. Attach a
`SourceCitation` (page + word-box + quote) to every extracted fact.

## Context / files
- `agent/src/copilot/documents/extract.py`.
- Reuse the Week-1 LLM client (`copilot/llm/client.py`) + prompts pattern.
- Use Anthropic structured output (`messages.parse`) bound to the PRP-02 schema
  so invalid output is rejected/retried at the tool layer, not post-hoc.
- Render PDF pages to images for the VLM; get word boxes from `pdfplumber`
  (PRP-03) and map each extracted value → its box for `field_or_chunk_id`.

## Contract
- `extract_lab(file_path) -> LabReport`
- `extract_intake(file_path) -> IntakeFacts`
- On low confidence / unreadable field: emit the field as `None` + flag, never a
  guessed value. `extraction_confidence` reflects the VLM's self-report clamped
  by how many required fields were grounded.
- Vision model: Opus 4.8 (configurable via settings).

## Validation gates
- [ ] Integration test uses a **stubbed VLM** (no live key) returning a fixed
      payload → asserts a valid `LabReport` with per-value citations.
- [ ] A malformed VLM payload → schema rejection (retry/typed error), never a
      silent pass (test).
- [ ] Every extracted value carries a `SourceCitation` with page + box + quote.
- [ ] The image-only doc still extracts (page-level citation acceptable when no
      word box exists) — degradation path test.
- [ ] Outbound VLM call has a tenacity timeout+retry; ruff + pytest green.

## Agent launch prompt
> Create `agent/src/copilot/documents/extract.py` with `extract_lab(file_path) ->
> LabReport` and `extract_intake(file_path) -> IntakeFacts`. Use the Week-1 LLM
> client and Anthropic structured output (`messages.parse`) bound to the PRP-02
> Pydantic schemas so raw VLM output cannot bypass validation. Render PDF pages to
> images for Claude vision (Opus 4.8, configurable) and use pdfplumber word boxes
> to attach a SourceCitation (page + bounding box + quoted value) to every
> extracted fact. Unreadable/low-confidence fields become None+flag, never
> guesses. Wrap the VLM call with a tenacity timeout+retry. Write integration
> tests with a **stubbed VLM** (no live API): valid payload → grounded LabReport;
> malformed payload → schema rejection; image-only doc → page-level citation.
> ruff + pytest green.
