# PRP M1-5 · LLM client + source-bound structured output (Sonnet)

**Milestone:** M1 · **Depends on:** M1-1 · **Blocks:** M1-7 · **Needs API key:** to BUILD no (SDK mocked in tests); to run the LIVE smoke YES

## Goal
The single LLM step of the walking skeleton (FR-8): given the minimum-necessary retrieved records, produce a `GroundedSummary` whose every `Claim` binds to `SourceRef`s that exist in the input. Grounding is enforced by output **shape** (structured output), not by prompting alone.

## Context
- Model per PRD §12 / config: **`claude-sonnet-5`** (cheapest; `ANTHROPIC_MODEL` in `.env`, already defaulted). Anthropic Python SDK is in `requirements.txt`.
- Follow the bundled **claude-api** skill for exact SDK surface — do not guess signatures. Use `AsyncAnthropic`; structured output via `client.messages.parse(..., output_format=GroundedSummary)` (or `output_config={"format": {...}}`) returning `.parsed_output`. Adaptive thinking optional.
- Minimum-necessary PHI to the LLM (NFR-4): send record ids, names, values, timestamps — the fields needed to reason and cite. Traces stay PHI-scrubbed (M0-6 `scrub_phi`); the LLM call itself carries the clinical values, over TLS.
- Own new files only: `llm/__init__.py`, `llm/client.py`, `llm/prompts.py`.

## Spec
- `llm/prompts.py`: the system prompt — synthesize a grounded "what changed + must-knows" summary; **every clinical claim must carry the `source_id`(s) of the record(s) it came from**; never assert a fact with no source; say "no data on file" when a field wasn't retrieved vs "no known …" when it was retrieved empty; rank by salience (an abnormal potassium outranks a normal one). Keep it tight (instruction-following model).
- `llm/client.py`:
  - `class LLMClient` with `async def summarize(critical_set: CriticalSet, deltas: Deltas) -> GroundedSummary`.
  - Builds the minimum-necessary user payload from the inputs (ids/names/values/timestamps only), calls Sonnet with the `GroundedSummary` output schema, returns the parsed model. Wrap in `trace("llm.summarize")` recording token counts (from `usage`) and model, **not** the payload.
  - `tenacity` retry on transient API errors; typed error on refusal / parse failure (surface, don't fabricate — FR-11).
- Config: read the API key + model from `Settings` (already wired). Fail with a clear message if the key is absent when a live call is attempted (do not crash at import).

## Validation
```bash
cd agent && . .venv/bin/activate && pip install -e . -q
pytest tests/test_llm_client.py -q     # unit: request shape (model=sonnet, schema attached) + GroundedSummary parsing, Anthropic HTTP mocked with respx — NO key needed
# LIVE (needs ANTHROPIC_API_KEY in agent/.env):
python -m copilot.llm.client --patient <seeded_id>   # end-to-end: retrieve -> Sonnet -> printed GroundedSummary with source_ids
```
Unit tests (no key): assert the request targets `claude-sonnet-5`, attaches the `GroundedSummary` schema, threads the correlation id, and parses a mocked response into `GroundedSummary`; a mocked refusal surfaces a typed error (no fabrication).

## Definition of done
The LLM client turns retrieved records into a schema-valid `GroundedSummary` with source-bound claims, builds + unit-tests **without** a key, and produces a real Sonnet summary once the key is present.
