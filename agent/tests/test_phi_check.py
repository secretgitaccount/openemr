"""Tests for the fail-closed PHI scanner (PRP-12, NFR-4).

Proves:

* the structured patterns (SSN, phone, MRN, e-mail, labelled DOB) and labelled
  names all fire, while allowlisted synthetic identifiers and ISO
  timestamps / uuids do not false-positive;
* scanning the committed eval artifacts + fixtures is clean (exit 0);
* a planted SSN makes ``main`` exit nonzero, a clean synthetic log exits 0;
* a missing scan target fails closed (nonzero).
"""

from __future__ import annotations

from copilot.scripts.phi_check import (
    default_targets,
    main,
    scan_file,
    scan_paths,
    scan_text,
)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


def test_detects_every_structured_pattern() -> None:
    text = "\n".join(
        [
            '{"ssn":"123-45-6789"}',
            '{"phone":"555-867-5309"}',
            '{"mrn":"MRN: 4472019"}',
            '{"email":"real.patient@gmail.com"}',
            '{"dob":"1990-01-02"}',
            '{"name":"Alice Realperson"}',
        ]
    )
    patterns = {h.pattern for h in scan_text(text)}
    assert {"ssn", "phone", "mrn", "email", "dob", "name"} <= patterns


def test_allowlisted_synthetic_identifiers_are_not_flagged() -> None:
    text = "\n".join(
        [
            '{"name":"Jordan Q. Testpatient"}',
            '{"mrn":"FAKE-000123"}',
            '{"dob":"1968-03-14"}',
            '{"provider":"Dr. Sam Fauxman, MD (Synthetic Clinic)"}',
        ]
    )
    assert scan_text(text) == []


def test_clean_structured_log_does_not_false_positive() -> None:
    # ISO timestamps, uuid correlation id and 2-group reference ranges are clean.
    text = (
        '{"event":"answer.assembled","claims":4,"dropped":0,'
        '"correlation_id":"3f2a1c8e-9b0d-4e77-8a12-1c2d3e4f5a6b",'
        '"ref":"70-99","level":"info","timestamp":"2026-07-13T16:59:12.123456Z"}'
    )
    assert scan_text(text) == []


def test_ssn_is_flagged_with_location() -> None:
    hits = scan_text('line one\nleak SSN 123-45-6789 here', path=None)
    assert len(hits) == 1
    assert hits[0].pattern == "ssn"
    assert hits[0].match == "123-45-6789"
    assert hits[0].line == 2


# ---------------------------------------------------------------------------
# File + path scanning
# ---------------------------------------------------------------------------


def test_committed_eval_artifacts_and_fixtures_are_clean() -> None:
    # The real repo artifacts the hook scans by default must be PHI-free.
    assert scan_paths(default_targets()) == []


def test_scan_file_skips_binary_suffix(tmp_path) -> None:
    fake_pdf = tmp_path / "scan.pdf"
    fake_pdf.write_bytes(b"SSN 123-45-6789")  # would match if read as text
    assert scan_file(fake_pdf) == []


# ---------------------------------------------------------------------------
# Process exit code (what the hook reads)
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_on_planted_ssn(tmp_path) -> None:
    log = tmp_path / "fake.log"
    log.write_text("2026-07-13 INFO request served\nnote: patient SSN 123-45-6789\n")
    assert main([str(log)]) == 1


def test_main_exits_zero_on_clean_synthetic_log(tmp_path) -> None:
    log = tmp_path / "clean.log"
    log.write_text(
        '{"event":"golden.case.ran","case_id":"evidence-hypertension-01",'
        '"correlation_id":"3f2a1c8e-9b0d-4e77-8a12-1c2d3e4f5a6b",'
        '"timestamp":"2026-07-13T16:59:12.200000Z"}\n'
    )
    assert main([str(log)]) == 0


def test_main_fails_closed_on_missing_target() -> None:
    assert main(["/no/such/path/anywhere.log"]) == 1


def test_main_default_targets_are_clean() -> None:
    assert main([]) == 0
