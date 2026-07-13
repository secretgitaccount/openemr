# PRP-03 — Synthetic demo documents

**Role:** backend-dev · **QA:** qa · **Depends on:** PRP-01 · **Blocks:** PRP-04 · **Needs key:** no

## Goal (atomic)
Generate the synthetic clinical documents the whole flow ingests — **text-layer
PDFs** so `pdfplumber` yields exact word bounding boxes for the citation overlay.
No real PHI. Deterministic (committed fixtures, not generated at test time).

## Context / files
- Generator script: `agent/tests/fixtures/documents/generate.py` (reportlab).
- Committed outputs: `agent/tests/fixtures/documents/`
  - `lab_cmp_lipid.pdf` — CMP + lipid panel (values incl. some abnormal flags)
  - `lab_hba1c_cbc.pdf` — HbA1c + CBC
  - `intake_form.pdf` — demographics, chief concern, meds, allergies, family hx
  - `lab_scanned.png` — one image-only (rasterized) doc to exercise graceful
    degradation (imperfect scan path)
- A `manifest.json` naming each file's `doc_type` + the ground-truth values (for
  eval assertions later).

## Validation gates
- [ ] `generate.py` reproduces byte-stable-enough PDFs (values match manifest).
- [ ] `pdfplumber` extracts words **with bboxes** from the text-layer PDFs (test).
- [ ] The `.png` has no text layer (confirms the degradation path is real).
- [ ] Values are clinically plausible and match the demo patient's conditions
      (diabetes/HTN/lipids) so RAG evidence lines up.
- [ ] No real patient data; names are obviously synthetic.

## Agent launch prompt
> Using reportlab, write `agent/tests/fixtures/documents/generate.py` that emits
> synthetic **text-layer** clinical PDFs: a CMP+lipid lab report, an HbA1c+CBC lab
> report, and a patient intake form (demographics, chief concern, current meds,
> allergies, family history). Include some abnormal lab values. Also rasterize one
> lab to `lab_scanned.png` (image-only, no text layer) for the graceful-degradation
> path. Commit the outputs plus a `manifest.json` mapping each file to its doc_type
> and ground-truth values. Values must be clinically plausible for a diabetes/HTN/
> lipids patient and fully synthetic. Add a test proving pdfplumber returns word
> bounding boxes from the PDFs and that the PNG has no text layer.
