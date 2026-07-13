"""Fail-closed PHI scanner for logs / traces / eval artifacts / fixtures (PRP-12).

The gate's second guardrail (NFR-4, FR-8): scan committed text for anything that
looks like Protected Health Information and **fail closed** — exit nonzero on any
hit, so a PHI leak can never ride a green build into the repo.

What counts as PHI here:

* structured identifiers — SSN, US phone, MRN, e-mail, and labelled DOB — matched
  by high-signal regexes chosen so ISO timestamps, uuid correlation ids and
  numeric reference ranges never false-positive; and
* **names in a labelled name/provider field that are not on the synthetic
  allowlist** — the only person-identifiers permitted anywhere in the repo are the
  obviously-fake fixtures ("Jordan Q. Testpatient", the synthetic clinician);
  anything else in a name field is treated as a real leak.

Usage::

    python -m copilot.scripts.phi_check                 # scan the default targets
    python -m copilot.scripts.phi_check path/to/file    # scan a specific path
    python -m copilot.scripts.phi_check dir1 file2 ...   # scan several paths

Exit code is ``0`` only when every scanned file is clean; any hit (or an
unreadable/missing path) exits ``1`` — fail closed.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PHI_PATTERNS",
    "NAME_LABEL_PATTERNS",
    "DOB_PATTERNS",
    "SYNTHETIC_ALLOWLIST",
    "PHIHit",
    "scan_text",
    "scan_file",
    "scan_paths",
    "default_targets",
    "main",
]

# .../agent/src/copilot/scripts/phi_check.py -> parents[3] == .../agent
_AGENT_ROOT = Path(__file__).resolve().parents[3]

#: Exact synthetic identifiers that are allowed to appear (the fixtures are
#: deliberately, obviously fake). A PHI-shaped token equal to one of these is not
#: a leak; anything else that matches a pattern is. Sourced from
#: ``tests/fixtures/documents/manifest.json`` (the one synthetic patient).
SYNTHETIC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "Jordan Q. Testpatient",
        "Jordan Testpatient",
        "FAKE-000123",
        "1968-03-14",
        "Dr. Sam Fauxman, MD (Synthetic Clinic)",
        "Dr. Sam Fauxman, MD",
        "Dr. Sam Fauxman",
        "Sam Fauxman",
    }
)

#: High-signal structured-identifier patterns. Deliberately narrow so ISO
#: timestamps (``2026-07-13``), uuid correlation ids and 2-group reference ranges
#: (``70-99``) never match.
PHI_PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b\d{3}-\d{3}-\d{4}\b"),
    "mrn": re.compile(r"\b(?:MRN[:#]?\s*\d{3,}|FAKE-\d{3,})\b", re.IGNORECASE),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
}

#: Labelled date-of-birth, requiring an actual date value so ``DOB: {template}``
#: placeholders in generators don't match.
DOB_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\"dob\"\s*:\s*\"(?P<value>\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\""
    ),
    re.compile(
        r"(?i)\b(?:DOB|date\s+of\s+birth)\b\s*[:=]\s*[\"']?"
        r"(?P<value>\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})"
    ),
)

#: Labelled name/provider fields (JSON and prose). The captured value is a hit
#: unless it is on the synthetic allowlist.
NAME_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\"(?:patient_?name|name|provider|clinician)\"\s*:\s*\"(?P<value>[^\"]+)\""),
    re.compile(r"(?i)\b(?:patient|provider|clinician)\s+name\s*[:=]\s*(?P<value>[^\n,;}\"]+)"),
)

#: File suffixes we never read (binary — a PDF/PNG can't be line-scanned).
_BINARY_SUFFIXES = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".pyc", ".pkl", ".faiss", ".bin", ".so"}
)

#: Directory names skipped while walking (noise, not committed artifacts).
_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"})


@dataclass(frozen=True)
class PHIHit:
    """One PHI match: where it was and what pattern caught it."""

    path: Path
    line: int
    pattern: str
    match: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}: [{self.pattern}] {self.match!r}"


def scan_text(text: str, *, path: Path | None = None) -> list[PHIHit]:
    """Scan a block of text line-by-line, returning one :class:`PHIHit` per match.

    Allowlisted synthetic identifiers are not reported; everything else that
    matches a PHI pattern is.
    """

    where = path if path is not None else Path("<text>")
    hits: list[PHIHit] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PHI_PATTERNS.items():
            for m in pattern.finditer(line):
                value = m.group(0)
                if value in SYNTHETIC_ALLOWLIST:
                    continue
                hits.append(PHIHit(path=where, line=i, pattern=name, match=value))
        for pattern in DOB_PATTERNS:
            for m in pattern.finditer(line):
                value = m.group("value")
                if value in SYNTHETIC_ALLOWLIST:
                    continue
                hits.append(PHIHit(path=where, line=i, pattern="dob", match=value))
        for pattern in NAME_LABEL_PATTERNS:
            for m in pattern.finditer(line):
                value = m.group("value").strip()
                if value in SYNTHETIC_ALLOWLIST:
                    continue
                hits.append(PHIHit(path=where, line=i, pattern="name", match=value))
    return hits


def scan_file(path: Path) -> list[PHIHit]:
    """Scan a single text file. Binary suffixes are skipped (returns ``[]``)."""

    if path.suffix.lower() in _BINARY_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Decoded bytes are not UTF-8 text (binary-ish) -> nothing to line-scan.
        return []
    except OSError:
        # Exists but unreadable (e.g. permissions): we cannot verify it is
        # PHI-free, so fail closed — surface a hit that makes the check exit 1.
        return [PHIHit(path=path, line=0, pattern="unreadable", match="cannot read file (fail-closed)")]
    return scan_text(text, path=path)


def _iter_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    files: list[Path] = []
    for path in sorted(target.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def scan_paths(paths: list[Path]) -> list[PHIHit]:
    """Scan every file under each path. Fails closed on a missing path.

    A path that does not exist raises :class:`FileNotFoundError` so the caller
    exits nonzero — a scan target that vanished is treated as a failure, never a
    silent pass.
    """

    hits: list[PHIHit] = []
    for target in paths:
        if not target.exists():
            raise FileNotFoundError(f"scan target does not exist: {target}")
        for path in _iter_files(target):
            hits.extend(scan_file(path))
    return hits


def default_targets() -> list[Path]:
    """The paths the hook/CI scan when no argument is given.

    The committed eval artifacts (results.json, baseline.json, cases) and the
    document fixtures (manifest + generator) — exactly the surfaces where PHI
    could leak into version control.
    """

    return [
        _AGENT_ROOT / "tests" / "eval" / "golden",
        _AGENT_ROOT / "tests" / "fixtures" / "documents",
    ]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    targets = [Path(a) for a in args] if args else default_targets()

    try:
        hits = scan_paths(targets)
    except FileNotFoundError as exc:
        print(f"phi_check: FAIL (fail-closed): {exc}")
        return 1

    scanned = ", ".join(str(t) for t in targets)
    if not hits:
        print(f"phi_check: PASS — no PHI found in {len(targets)} target(s): {scanned}")
        return 0

    print(f"phi_check: FAIL — {len(hits)} PHI hit(s):")
    for hit in hits:
        print(f"  - {hit.describe()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
