"""Tests for the synthetic demo documents (PRP-03).

Proves the committed fixtures satisfy the ingestion contract:
- the text-layer PDFs yield ``pdfplumber`` words **with bounding boxes** (needed
  for the citation overlay),
- the ``.png`` has **no text layer** (the graceful-degradation / imperfect-scan
  path is genuinely exercised), and
- every ground-truth value in ``manifest.json`` actually appears in the rendered
  document, so later eval assertions can trust the manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pdfplumber
import pytest
from PIL import Image

DOCS_DIR = Path(__file__).resolve().parent / "fixtures" / "documents"
TEXT_LAYER_PDFS = ("lab_cmp_lipid.pdf", "lab_hba1c_cbc.pdf", "intake_form.pdf")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((DOCS_DIR / "manifest.json").read_text())


def _pdf_text(name: str) -> str:
    with pdfplumber.open(DOCS_DIR / name) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def test_all_fixtures_committed() -> None:
    for name in (*TEXT_LAYER_PDFS, "lab_scanned.png", "manifest.json"):
        p = DOCS_DIR / name
        assert p.exists(), f"missing fixture {name} (run generate.py)"
        assert p.stat().st_size > 0


@pytest.mark.parametrize("name", TEXT_LAYER_PDFS)
def test_text_layer_pdf_yields_words_with_bboxes(name: str) -> None:
    with pdfplumber.open(DOCS_DIR / name) as pdf:
        words = pdf.pages[0].extract_words()
    assert words, f"{name} has no extractable words — text layer missing"
    for w in words:
        for key in ("x0", "x1", "top", "bottom"):
            assert key in w, f"{name} word {w['text']!r} missing bbox key {key}"
        assert w["x1"] > w["x0"] and w["bottom"] > w["top"]


def test_png_has_no_text_layer() -> None:
    png = DOCS_DIR / "lab_scanned.png"
    # It is a real raster image...
    with Image.open(png) as im:
        assert im.format == "PNG"
        assert im.size[0] > 0 and im.size[1] > 0
    # ...and not a PDF, so there is no text layer to extract (degradation path).
    with pytest.raises(Exception):
        with pdfplumber.open(png) as pdf:
            pdf.pages[0].extract_words()


def test_manifest_matches_rendered_lab_values(manifest: dict) -> None:
    for name in ("lab_cmp_lipid.pdf", "lab_hba1c_cbc.pdf"):
        text = _pdf_text(name)
        entry = manifest["documents"][name]
        assert entry["doc_type"] == "lab_report"
        assert entry["text_layer"] is True
        for analyte, gt in entry["results"].items():
            assert analyte in text, f"{name}: analyte {analyte!r} not rendered"
            assert gt["value"] in text, f"{name}: value {gt['value']!r} for {analyte} not rendered"
        # Every declared abnormal has an H/L flag and actually appears.
        for analyte in entry["abnormal"]:
            assert entry["results"][analyte]["flag"] in ("H", "L")
        assert entry["abnormal"], f"{name}: expected some abnormal values"


def test_manifest_matches_rendered_intake(manifest: dict) -> None:
    text = _pdf_text("intake_form.pdf")
    entry = manifest["documents"]["intake_form.pdf"]
    assert entry["doc_type"] == "intake_form"
    assert manifest["patient"]["name"] in text
    assert entry["chief_concern"].split(";")[0] in text
    for med in entry["medications"]:
        assert med in text, f"medication {med!r} not rendered"
    for allergy in entry["allergies"]:
        assert allergy in text, f"allergy {allergy!r} not rendered"
    for fam in entry["family_history"]:
        assert fam in text, f"family history {fam!r} not rendered"


def test_manifest_png_marked_no_text_layer(manifest: dict) -> None:
    entry = manifest["documents"]["lab_scanned.png"]
    assert entry["text_layer"] is False
    assert entry["rasterized_from"] == "lab_cmp_lipid.pdf"


def test_patient_is_obviously_synthetic(manifest: dict) -> None:
    patient = manifest["patient"]
    assert "Testpatient" in patient["name"]
    assert patient["mrn"].startswith("FAKE-")
