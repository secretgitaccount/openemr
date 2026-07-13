# PRP-00 — FHIR-write spike

**Role:** Explore (read-only) · **QA:** n/a (verdict doc) · **Depends on:** — · **Blocks:** PRP-05, PRP-06 · **Needs key:** no

## Goal (atomic)
Determine, empirically, how the agent can write back to OpenEMR: (a) store a
source document and (b) persist a derived Observation — **without** creating
duplicate/untraceable records. Produce a written verdict that decides PRP-05's
write path. **No product code**; a throwaway probe script is fine.

## Context / files
- Live target: Railway OpenEMR (reachable now), creds in `agent/.env`
  (`OPENEMR_*`). FHIR base = `OPENEMR_FHIR_BASE`, REST base derivable.
- Reuse Week 1 OAuth client (`agent/src/copilot/openemr/oauth.py`) to get a token.
- Reference: OpenEMR `FHIR_README.md`, `API_README.md` at repo root.

## What to probe (answer each with the actual HTTP status + body shape)
1. Does `POST {fhir_base}/Observation` succeed with a minimal valid Observation?
   What scope is required? Does it return an `id`?
2. Does `POST {fhir_base}/DocumentReference` (+ `Binary`) accept a source PDF?
   Or is the REST `POST /api/patient/{id}/document` the only working doc path?
3. What identifiers let us later find/dedupe what we wrote (identifier, tag)?
4. Which OAuth scopes must the client have; does the current client have them?

## Validation gates
- [ ] A markdown verdict written to `PRPs/_spikes/PRP-00-result.md` covering all
      four probes with real status codes and example request/response bodies.
- [ ] A clear **recommendation**: FHIR-native writes vs. REST-document fallback
      (or hybrid), with the exact endpoints + scopes PRP-05 should target.
- [ ] Dedup strategy named (how a re-run of `attach_and_extract` avoids dupes).
- [ ] No writes left dangling — clean up probe records or list their ids.

## Agent launch prompt
> You are probing OpenEMR's write API to decide our document-ingestion write
> path. Read `agent/.env` for OpenEMR creds and `FHIR_README.md`/`API_README.md`.
> Using the Week-1 OAuth client to obtain a token, empirically test: POST
> Observation, POST DocumentReference/Binary, and REST `/api/patient/{id}/document`
> against the **live Railway OpenEMR** (`OPENEMR_FHIR_BASE`). For each, record the
> real HTTP status and body. Determine required scopes and whether the current
> client has them. Write a verdict to `PRPs/_spikes/PRP-00-result.md` with a
> concrete recommendation (FHIR-native vs REST fallback vs hybrid), the exact
> endpoints/scopes PRP-05 should use, and a dedup strategy. Use only synthetic
> data; clean up or list any test records you create. Do not write product code.
