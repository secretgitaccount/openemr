# PRP-13 — UI: upload + PDF bbox overlay + click-to-source

**Role:** frontend-dev · **QA:** qa · **Depends on:** PRP-06, PRP-10 · **Blocks:** — · **Needs key:** no (drives the running agent)

## Goal (atomic)
Make the multimodal flow visible and grounded: upload a document, see extracted
values, and **click any claim's citation to highlight the exact PDF region** it
came from (FR-7). Render the answer with record-facts vs guideline-evidence
visibly separated.

## Context / files owned
- `agent/src/copilot/ui/index.html` and `ui/**` only. Vanilla — no framework, no
  external hosts (CSP-clean). Consumes PRP-06 `IngestResult` + PRP-10 `W2Answer`.
- PDF page rendered to PNG server-side; overlay boxes as absolutely-positioned
  divs using the `SourceCitation` page + bbox.

## Contract / behavior
- Upload control → `POST /patients/{id}/documents`; show extracted values with a
  confidence indicator.
- Answer view: `answer_claims` each render their citation chip; **record facts**
  and **guideline evidence** in separate, labeled sections.
- Click a citation → for a `lab_pdf`/`intake_form` source, scroll the document
  preview and highlight the word-box; for a `guideline` source, reveal the
  evidence snippet + source attribution.
- Image-only doc: page-level highlight (graceful degradation).

## Validation
```bash
# drive the running agent; where feasible use the Selenium/Panther harness
```
- Behavioral: upload a fixture doc → extracted values with overlays appear;
  clicking a lab claim highlights the correct box; clicking a guideline claim
  shows the snippet; record vs guideline sections are visually distinct.
- No CSP violations (no external script/font/CDN); no PHI persisted client-side.
- Screenshot(s) attached for the demo; degrades cleanly on the image-only doc.

## Builder prompt (frontend-dev → qa)
> Extend `ui/index.html` (vanilla, no external hosts) with: a document-upload
> control posting to `POST /patients/{id}/documents` and showing extracted values
> + confidence; an answer view rendering PRP-10 `W2Answer` with **record facts**
> and **guideline evidence** in separate labeled sections; and click-to-source —
> clicking a claim's citation highlights the exact PDF word-box (rendered PNG +
> positioned divs from the SourceCitation bbox) for document sources, or reveals
> the snippet for guideline sources. Degrade to page-level highlight for the
> image-only doc. Verify behaviorally against the running agent (Selenium/Panther
> screenshot). No CSP violations, no client-side PHI. Hand to qa.
