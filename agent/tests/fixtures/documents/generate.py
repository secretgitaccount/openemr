"""Generate synthetic, text-layer clinical demo documents (PRP-03).

Emits deterministic, committed fixtures the whole ingestion flow consumes:

- ``lab_cmp_lipid.pdf``  — CMP + lipid panel (with abnormal flags)
- ``lab_hba1c_cbc.pdf``  — HbA1c + CBC
- ``intake_form.pdf``    — demographics, chief concern, meds, allergies, family hx
- ``lab_scanned.png``    — one lab rasterized to an image-only file (NO text
                            layer) to exercise the graceful-degradation path
- ``manifest.json``      — each file's ``doc_type`` + ground-truth values for
                            later eval assertions

Design notes
------------
- **Text-layer PDFs.** Rendered with reportlab so ``pdfplumber`` yields exact
  word bounding boxes for the citation overlay. The PNG is drawn pixel-by-pixel
  with Pillow so it genuinely has no extractable text layer.
- **Single source of truth.** The document content and the manifest are built
  from the same in-module data structures, so the manifest can never drift from
  what was actually rendered.
- **Fully synthetic.** Obviously-fake patient ("Jordan Q. Testpatient",
  MRN ``FAKE-000123``); clinically plausible for a diabetes / HTN / lipids
  patient so downstream RAG evidence lines up. No real PHI.

Run directly to (re)produce the committed fixtures::

    python tests/fixtures/documents/generate.py
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_DIR = Path(__file__).resolve().parent

# --- Synthetic demo patient (obviously fake — no real PHI) --------------------
PATIENT: dict[str, str] = {
    "name": "Jordan Q. Testpatient",
    "mrn": "FAKE-000123",
    "dob": "1968-03-14",
    "sex": "Male",
    "collected": "2026-06-30",
    "provider": "Dr. Sam Fauxman, MD (Synthetic Clinic)",
}

# Each lab row: (analyte, value, unit, reference range, flag)  flag in {"", "H", "L"}
CMP_ROWS: list[tuple[str, str, str, str, str]] = [
    ("Glucose", "168", "mg/dL", "70-99", "H"),
    ("BUN", "18", "mg/dL", "7-20", ""),
    ("Creatinine", "1.1", "mg/dL", "0.6-1.3", ""),
    ("eGFR", "78", "mL/min/1.73", ">=60", ""),
    ("Sodium", "139", "mmol/L", "136-145", ""),
    ("Potassium", "4.4", "mmol/L", "3.5-5.1", ""),
    ("Chloride", "102", "mmol/L", "98-107", ""),
    ("CO2", "25", "mmol/L", "22-29", ""),
    ("Calcium", "9.4", "mg/dL", "8.5-10.2", ""),
    ("Total Protein", "7.0", "g/dL", "6.0-8.3", ""),
    ("Albumin", "4.2", "g/dL", "3.5-5.0", ""),
    ("Total Bilirubin", "0.6", "mg/dL", "0.1-1.2", ""),
    ("Alkaline Phosphatase", "78", "U/L", "40-129", ""),
    ("AST", "24", "U/L", "10-40", ""),
    ("ALT", "30", "U/L", "7-56", ""),
]

LIPID_ROWS: list[tuple[str, str, str, str, str]] = [
    ("Total Cholesterol", "232", "mg/dL", "<200", "H"),
    ("LDL Cholesterol", "155", "mg/dL", "<100", "H"),
    ("HDL Cholesterol", "38", "mg/dL", ">40", "L"),
    ("Triglycerides", "215", "mg/dL", "<150", "H"),
    ("Non-HDL Cholesterol", "194", "mg/dL", "<130", "H"),
]

HBA1C_ROWS: list[tuple[str, str, str, str, str]] = [
    ("Hemoglobin A1c", "8.2", "%", "<5.7", "H"),
    ("Estimated Average Glucose", "189", "mg/dL", "<117", "H"),
]

CBC_ROWS: list[tuple[str, str, str, str, str]] = [
    ("WBC", "6.8", "10^3/uL", "4.0-11.0", ""),
    ("RBC", "4.7", "10^6/uL", "4.2-5.9", ""),
    ("Hemoglobin", "14.2", "g/dL", "13.5-17.5", ""),
    ("Hematocrit", "42.0", "%", "41-53", ""),
    ("MCV", "89", "fL", "80-100", ""),
    ("Platelets", "245", "10^3/uL", "150-400", ""),
]

INTAKE: dict[str, object] = {
    "chief_concern": (
        "Follow-up for type 2 diabetes and high blood pressure; "
        "occasional blurry vision."
    ),
    "blood_pressure": "148/92 mmHg",
    "medications": [
        "Metformin 1000 mg PO BID",
        "Lisinopril 20 mg PO daily",
        "Atorvastatin 40 mg PO daily",
        "Amlodipine 5 mg PO daily",
    ],
    "allergies": ["Penicillin (hives)"],
    "family_history": [
        "Father: type 2 diabetes, myocardial infarction at 62",
        "Mother: hypertension",
    ],
}


def _fmt_row(row: tuple[str, str, str, str, str]) -> str:
    """One fixed-width-ish lab line: 'Analyte: value unit  (ref)  [FLAG]'."""
    analyte, value, unit, ref, flag = row
    flag_txt = f"  [{flag}]" if flag else ""
    return f"{analyte}: {value} {unit}  (ref {ref}){flag_txt}"


def _cmp_lipid_lines() -> list[str]:
    lines: list[str] = [
        "SYNTHETIC CLINICAL LABORATORY — DEMO FIXTURE (NOT REAL)",
        "Comprehensive Metabolic Panel + Lipid Panel",
        "",
        f"Patient: {PATIENT['name']}    MRN: {PATIENT['mrn']}",
        f"DOB: {PATIENT['dob']}    Sex: {PATIENT['sex']}",
        f"Collected: {PATIENT['collected']}    Ordering provider: {PATIENT['provider']}",
        "",
        "Comprehensive Metabolic Panel",
        *[_fmt_row(r) for r in CMP_ROWS],
        "",
        "Lipid Panel",
        *[_fmt_row(r) for r in LIPID_ROWS],
        "",
        "Flags: [H] above reference, [L] below reference.",
        "Interpretation: hyperglycemia and dyslipidemia consistent with",
        "poorly-controlled type 2 diabetes and mixed hyperlipidemia.",
    ]
    return lines


def _hba1c_cbc_lines() -> list[str]:
    lines: list[str] = [
        "SYNTHETIC CLINICAL LABORATORY — DEMO FIXTURE (NOT REAL)",
        "Hemoglobin A1c + Complete Blood Count",
        "",
        f"Patient: {PATIENT['name']}    MRN: {PATIENT['mrn']}",
        f"DOB: {PATIENT['dob']}    Sex: {PATIENT['sex']}",
        f"Collected: {PATIENT['collected']}    Ordering provider: {PATIENT['provider']}",
        "",
        "Glycemic Control",
        *[_fmt_row(r) for r in HBA1C_ROWS],
        "",
        "Complete Blood Count",
        *[_fmt_row(r) for r in CBC_ROWS],
        "",
        "Flags: [H] above reference, [L] below reference.",
        "Interpretation: A1c 8.2% indicates suboptimal glycemic control;",
        "CBC within normal limits.",
    ]
    return lines


def _intake_lines() -> list[str]:
    meds = INTAKE["medications"]
    allergies = INTAKE["allergies"]
    fam = INTAKE["family_history"]
    assert isinstance(meds, list) and isinstance(allergies, list) and isinstance(fam, list)
    lines: list[str] = [
        "SYNTHETIC CLINIC — PATIENT INTAKE FORM (DEMO FIXTURE, NOT REAL)",
        "",
        "Demographics",
        f"Name: {PATIENT['name']}",
        f"MRN: {PATIENT['mrn']}    DOB: {PATIENT['dob']}    Sex: {PATIENT['sex']}",
        f"Visit date: {PATIENT['collected']}    Provider: {PATIENT['provider']}",
        "",
        "Chief Concern",
        str(INTAKE["chief_concern"]),
        f"Blood pressure at intake: {INTAKE['blood_pressure']}",
        "",
        "Current Medications",
        *[f"- {m}" for m in meds],
        "",
        "Allergies",
        *[f"- {a}" for a in allergies],
        "",
        "Family History",
        *[f"- {f}" for f in fam],
    ]
    return lines


def _render_pdf(path: Path, lines: list[str]) -> None:
    """Render lines as a real text-layer PDF (pdfplumber-extractable words)."""
    width, height = letter
    c = canvas.Canvas(str(path), pagesize=letter)
    # Deterministic metadata so re-runs stay stable.
    c.setTitle(path.stem)
    c.setAuthor("PRP-03 synthetic fixture generator")
    c.setSubject("Synthetic clinical demo document — no real PHI")
    left = 54
    top = height - 54
    leading = 18
    y = top
    for line in lines:
        if line.startswith("SYNTHETIC") or line in (
            "Comprehensive Metabolic Panel + Lipid Panel",
            "Hemoglobin A1c + Complete Blood Count",
        ):
            c.setFont("Helvetica-Bold", 12)
        elif line in (
            "Comprehensive Metabolic Panel",
            "Lipid Panel",
            "Glycemic Control",
            "Complete Blood Count",
            "Demographics",
            "Chief Concern",
            "Current Medications",
            "Allergies",
            "Family History",
        ):
            c.setFont("Helvetica-Bold", 11)
        else:
            c.setFont("Helvetica", 10)
        c.drawString(left, y, line)
        y -= leading
    c.showPage()
    c.save()


def _render_png(path: Path, lines: list[str]) -> None:
    """Rasterize lines to an image-only PNG — genuinely NO text layer.

    Drawn pixel-by-pixel with Pillow (not a rendered PDF), so downstream text
    extraction finds nothing and must fall back to the imperfect-scan path.
    """
    width, height = 850, 1100
    img = Image.new("RGB", (width, height), color=(248, 248, 244))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=15)
        font_bold = ImageFont.load_default(size=17)
    except TypeError:  # very old Pillow without size arg
        font = font_bold = ImageFont.load_default()
    left, top, leading = 40, 40, 22
    y = top
    for line in lines:
        use = font_bold if line.startswith("SYNTHETIC") else font
        draw.text((left, y), line, fill=(20, 20, 20), font=use)
        y += leading
    # Faint scan-like texture so it reads as an imperfect scan, not clean render.
    for gy in range(0, height, 4):
        draw.line([(0, gy), (width, gy)], fill=(243, 243, 239), width=1)
    img.save(path, format="PNG")


def _ground_truth(rows: list[tuple[str, str, str, str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for analyte, value, unit, ref, flag in rows:
        out[analyte] = {"value": value, "unit": unit, "ref": ref, "flag": flag}
    return out


def build_manifest() -> dict[str, object]:
    """Assemble the manifest from the same data used to render the documents."""
    return {
        "patient": dict(PATIENT),
        "documents": {
            "lab_cmp_lipid.pdf": {
                "doc_type": "lab_report",
                "text_layer": True,
                "panels": ["comprehensive_metabolic_panel", "lipid_panel"],
                "results": {**_ground_truth(CMP_ROWS), **_ground_truth(LIPID_ROWS)},
                "abnormal": [r[0] for r in CMP_ROWS + LIPID_ROWS if r[4]],
            },
            "lab_hba1c_cbc.pdf": {
                "doc_type": "lab_report",
                "text_layer": True,
                "panels": ["hba1c", "complete_blood_count"],
                "results": {**_ground_truth(HBA1C_ROWS), **_ground_truth(CBC_ROWS)},
                "abnormal": [r[0] for r in HBA1C_ROWS + CBC_ROWS if r[4]],
            },
            "intake_form.pdf": {
                "doc_type": "intake_form",
                "text_layer": True,
                "chief_concern": INTAKE["chief_concern"],
                "blood_pressure": INTAKE["blood_pressure"],
                "medications": INTAKE["medications"],
                "allergies": INTAKE["allergies"],
                "family_history": INTAKE["family_history"],
            },
            "lab_scanned.png": {
                "doc_type": "lab_report",
                "text_layer": False,
                "note": "Image-only rasterization of lab_cmp_lipid for the "
                "graceful-degradation (imperfect scan) path.",
                "rasterized_from": "lab_cmp_lipid.pdf",
            },
        },
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _render_pdf(OUT_DIR / "lab_cmp_lipid.pdf", _cmp_lipid_lines())
    _render_pdf(OUT_DIR / "lab_hba1c_cbc.pdf", _hba1c_cbc_lines())
    _render_pdf(OUT_DIR / "intake_form.pdf", _intake_lines())
    _render_png(OUT_DIR / "lab_scanned.png", _cmp_lipid_lines())
    manifest = build_manifest()
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote fixtures to {OUT_DIR}:")
    for name in (
        "lab_cmp_lipid.pdf",
        "lab_hba1c_cbc.pdf",
        "intake_form.pdf",
        "lab_scanned.png",
        "manifest.json",
    ):
        p = OUT_DIR / name
        print(f"  {name}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
