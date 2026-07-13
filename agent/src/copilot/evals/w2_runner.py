"""The graded golden-set eval runner (PRP-11, FR-8).

This is the **offline, deterministic** graded eval for the Week-2 flow: a fixed
set of 50 synthetic cases (``tests/eval/golden/cases/*.json``) exercised against
the *real* answer-assembly pipeline (:func:`copilot.graph.answer.build_answer` +
the Week-1 verification gate) with a **stubbed model**, then scored by five
boolean rubrics:

* ``schema_valid`` — every structured artifact the pipeline produced validated
  against its Pydantic contract (the schema is the trust gate).
* ``citation_present`` — no surfaced claim escaped without a citation.
* ``factually_consistent`` — every surfaced claim's source resolves to the
  evidence actually available (the gate dropped anything ungrounded).
* ``safe_refusal`` — a case that should refuse produced a safe, PHI-free refusal
  and surfaced no answer.
* ``no_phi_in_logs`` — the logs emitted while running the case contain no PHI.
  This evaluator **actually scans the emitted log lines** (see
  :func:`scan_for_phi`).

Each :class:`GoldenCase` declares the *expected* value of all five rubrics; the
runner measures the *actual* value from the pipeline outcome and a category
"passes" for a case when measured == expected. :func:`run_golden` writes a
committed, **machine-comparable** ``results.json`` (stable key order, no
timestamps) so PRP-12 can diff a baseline and block a >5% regression.

Everything here runs with **no Anthropic key and no network**: the model is a
deterministic stub synthesizer and the evidence is drawn from the repo's fixed
guideline corpus and document manifest. The optional Langfuse push reuses the
Week-1 dataset pattern (:mod:`copilot.evals.langfuse_eval`) and is a no-op unless
Langfuse keys are configured.

Run it::

    cd agent && .venv/bin/python -m copilot.evals.w2_runner
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from copilot.documents.schemas import (
    CitedList,
    CitedText,
    IntakeDemographics,
    IntakeFacts,
    LabObservation,
    LabReport,
    SourceCitation,
)
from copilot.graph.answer import W2Answer, build_answer
from copilot.graph.state import GraphResult
from copilot.llm.client import LLMError
from copilot.logging import configure_logging, get_logger
from copilot.rag.chunk import GuidelineChunk, load_corpus
from copilot.rag.retrieve import GuidelineEvidence
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary

__all__ = [
    "GoldenCase",
    "CaseOutcome",
    "CaseResult",
    "EvalReport",
    "RUBRIC_NAMES",
    "RUBRICS",
    "scan_for_phi",
    "eval_schema_valid",
    "eval_citation_present",
    "eval_factually_consistent",
    "eval_safe_refusal",
    "eval_no_phi_in_logs",
    "load_cases",
    "run_golden",
    "GOLDEN_DIR",
    "CASES_DIR",
    "RESULTS_PATH",
]

logger = get_logger(__name__)

# --- repo paths (no DB; everything resolves from committed files) ----------

# .../agent/src/copilot/evals/w2_runner.py -> parents[3] == .../agent
_AGENT_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = _AGENT_ROOT / "tests" / "eval" / "golden"
CASES_DIR = GOLDEN_DIR / "cases"
RESULTS_PATH = GOLDEN_DIR / "results.json"
_MANIFEST_PATH = _AGENT_ROOT / "tests" / "fixtures" / "documents" / "manifest.json"

#: The five boolean rubric categories every case must declare and the runner
#: scores. Order is stable so per-category output is deterministic.
RUBRIC_NAMES: tuple[str, ...] = (
    "schema_valid",
    "citation_present",
    "factually_consistent",
    "safe_refusal",
    "no_phi_in_logs",
)

_KINDS = ("extraction", "evidence", "citation", "refusal", "missing_data")


# ---------------------------------------------------------------------------
# Case contract
# ---------------------------------------------------------------------------


class GoldenCase(BaseModel):
    """One graded golden case, loaded verbatim from a repo JSON file.

    ``input`` is the doc ref (extraction) or the query (evidence / citation /
    refusal / missing-data). ``expected`` declares the expected boolean outcome
    of **all five** rubric categories — the validator rejects a case that omits
    or adds one, so "each case declares all five" is enforced at load.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    kind: Literal["extraction", "evidence", "citation", "refusal", "missing_data"]
    input: dict[str, Any]
    expected_behavior: str = Field(min_length=1)
    expected: dict[str, bool]

    @field_validator("expected")
    @classmethod
    def _declares_all_five(cls, value: dict[str, bool]) -> dict[str, bool]:
        keys = set(value)
        want = set(RUBRIC_NAMES)
        if keys != want:
            missing = want - keys
            extra = keys - want
            raise ValueError(
                f"expected must declare exactly the five rubrics; "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        return value

    @property
    def query(self) -> str:
        """The clinical question for this case (synthesised for extraction docs)."""

        q = self.input.get("query")
        if q:
            return str(q)
        return f"Summarize document {self.input.get('doc', 'attachment')}"


def load_cases(cases_dir: Path = CASES_DIR) -> list[GoldenCase]:
    """Load every golden case from ``cases_dir/*.json`` (repo files, no DB).

    Each file is a JSON array of case objects. Case ids must be globally unique.
    """

    cases: list[GoldenCase] = []
    for path in sorted(cases_dir.glob("*.json")):
        raw = json.loads(path.read_text())
        if not isinstance(raw, list):
            raise ValueError(f"{path.name}: expected a JSON array of cases")
        cases.extend(GoldenCase(**item) for item in raw)

    ids = [c.id for c in cases]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate golden case ids: {sorted(dupes)}")
    return cases


# ---------------------------------------------------------------------------
# Synthetic evidence builders (manifest ground truth + guideline corpus)
# ---------------------------------------------------------------------------

_FLAG_MAP = {"H": "high", "L": "low", "": "normal"}


def _manifest() -> dict[str, Any]:
    return json.loads(_MANIFEST_PATH.read_text())


def _lab_from_manifest(doc_id: str, *, malformed: bool = False) -> LabReport:
    """Build a validated :class:`LabReport` from the manifest ground truth.

    ``lab_scanned.png`` (an image-only rasterization with no text layer) reuses
    the CMP/lipid results but degrades every citation to page-level (no bbox),
    modelling the graceful-degradation path. ``malformed=True`` sets an
    out-of-range confidence so the schema rejects the extraction (the negative
    "schema is the gate" case).
    """

    docs = _manifest()["documents"]
    box_grounded = doc_id != "lab_scanned.png"
    results_doc = "lab_cmp_lipid.pdf" if doc_id == "lab_scanned.png" else doc_id
    results = docs[results_doc]["results"]

    observations: list[LabObservation] = []
    for name, row in results.items():
        observations.append(
            LabObservation(
                test_name=name,
                value=row["value"],
                unit=row["unit"],
                reference_range=row["ref"],
                collection_date=None,
                abnormal_flag=_FLAG_MAP.get(row["flag"], "unknown"),
                citation=SourceCitation(
                    source_type="lab_pdf",
                    source_id=doc_id,
                    page_or_section="page 1",
                    field_or_chunk_id=f"bbox={name}" if box_grounded else None,
                    quote_or_value=str(row["value"]),
                ),
            )
        )

    return LabReport(
        patient_ref=SourceRef(resource_type="Patient", id="synthetic-patient"),
        report_date=None,
        observations=observations,
        # 1.5 is out of the [0, 1] bound -> ValidationError (rejected at the schema).
        extraction_confidence=1.5 if malformed else 0.9,
        source=SourceCitation(
            source_type="lab_pdf",
            source_id=doc_id,
            page_or_section="page 1",
            field_or_chunk_id=None,
            quote_or_value="Laboratory Report",
        ),
    )


def _intake_from_manifest(*, malformed: bool = False) -> IntakeFacts:
    """Build validated :class:`IntakeFacts` from the manifest intake form."""

    docs = _manifest()["documents"]
    intake = docs["intake_form.pdf"]

    def cite(quote: str) -> SourceCitation:
        return SourceCitation(
            source_type="intake_form",
            source_id="intake_form.pdf",
            page_or_section="page 1",
            field_or_chunk_id="intake-field",
            quote_or_value=quote,
        )

    demographics = IntakeDemographics(
        name="Patient",
        dob=None,
        sex=None,
        citation=cite("Patient Information"),
    )
    return IntakeFacts(
        demographics=demographics,
        chief_concern=CitedText(text=intake["chief_concern"], citation=cite("Reason for visit")),
        current_medications=CitedList(
            items=list(intake["medications"]), citation=cite("Current Medications")
        ),
        allergies=CitedList(items=list(intake["allergies"]), citation=cite("Allergies")),
        family_history=CitedList(
            items=list(intake["family_history"]), citation=cite("Family History")
        ),
        # 1.5 is out of the [0, 1] bound -> ValidationError.
        extraction_confidence=1.5 if malformed else 0.9,
        source=cite("Patient Intake Form"),
    )


def _build_extracted(inp: dict[str, Any]) -> list[LabReport | IntakeFacts]:
    """Build the extracted document model(s) for an extraction/citation case."""

    doc = inp["doc"]
    malformed = bool(inp.get("malformed", False))
    if doc == "intake_form.pdf":
        return [_intake_from_manifest(malformed=malformed)]
    return [_lab_from_manifest(doc, malformed=malformed)]


# --- guideline corpus evidence (deterministic keyword ranking, no models) ---

_CORPUS: list[GuidelineChunk] = load_corpus()


def _topic_of(chunk: GuidelineChunk) -> str:
    return chunk.chunk_id.split("::", 1)[0]


_CORPUS_BY_TOPIC: dict[str, list[GuidelineChunk]] = {}
for _chunk in _CORPUS:
    _CORPUS_BY_TOPIC.setdefault(_topic_of(_chunk), []).append(_chunk)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _evidence_for(query: str, topic: str | None, k: int = 2) -> list[GuidelineEvidence]:
    """Pick the top-``k`` corpus chunks for ``query`` by keyword overlap.

    Deterministic and offline: a stand-in for the PRP-08 hybrid retriever that
    needs no model download, so the golden run stays fast and reproducible while
    still exercising the real :class:`GuidelineChunk` -> citation code path.
    """

    pool = _CORPUS_BY_TOPIC.get(topic or "", _CORPUS)
    q = _words(query)
    ranked = sorted(
        pool,
        key=lambda c: (-len(q & _words(f"{c.section} {c.text}")), c.chunk_id),
    )
    top = ranked[:k]
    n = len(top)
    return [
        GuidelineEvidence(chunk=c, score=float(n - i), retriever="hybrid")
        for i, c in enumerate(top)
    ]


# ---------------------------------------------------------------------------
# Stub model (deterministic synthesizers — no Anthropic call)
# ---------------------------------------------------------------------------

_Synth = Callable[
    [str, list[SourceCitation], list[SourceCitation]], Awaitable[GroundedSummary]
]


def _claim_for(citation: SourceCitation) -> Claim:
    """A grounded claim citing exactly ``citation`` by (source_type, source_id)."""

    locator = citation.field_or_chunk_id or citation.page_or_section or citation.source_id
    return Claim(
        text=f"[{citation.source_type}] {locator}: {citation.quote_or_value}",
        sources=[SourceRef(resource_type=citation.source_type, id=citation.source_id)],
    )


def _cited_claims(
    record_facts: list[SourceCitation], guideline_evidence: list[SourceCitation]
) -> list[Claim]:
    return [_claim_for(c) for c in (list(record_facts)[:4] + list(guideline_evidence)[:4])]


async def _synth_grounded(
    question: str,
    record_facts: list[SourceCitation],
    guideline_evidence: list[SourceCitation],
) -> GroundedSummary:
    """Emit only claims grounded in the supplied evidence (the honest path)."""

    return GroundedSummary(
        headline=f"Answer: {question[:80]}",
        must_knows=_cited_claims(record_facts, guideline_evidence),
        whats_changed=[],
        caveats=[],
    )


async def _synth_with_ungrounded(
    question: str,
    record_facts: list[SourceCitation],
    guideline_evidence: list[SourceCitation],
) -> GroundedSummary:
    """Emit grounded claims plus one fabricated claim the gate MUST drop.

    Proves ``citation_present`` / ``factually_consistent`` hold *after* the gate:
    the fabricated claim cites a chunk id that is not in the available evidence,
    so verification removes it before it can be surfaced.
    """

    claims = _cited_claims(record_facts, guideline_evidence)
    claims.append(
        Claim(
            text="Fabricated, unsupported claim the verification gate must drop.",
            sources=[SourceRef(resource_type="guideline", id="__nonexistent_chunk__")],
        )
    )
    return GroundedSummary(
        headline=f"Answer: {question[:80]}",
        must_knows=claims,
        whats_changed=[],
        caveats=[],
    )


async def _synth_caveats_only(
    question: str,
    record_facts: list[SourceCitation],
    guideline_evidence: list[SourceCitation],
) -> GroundedSummary:
    """Missing-data path: surface no claim, only a plain-language caveat."""

    return GroundedSummary(
        headline="Insufficient evidence to answer this question.",
        must_knows=[],
        whats_changed=[],
        caveats=[
            "The available records and guidelines do not contain enough "
            "information to answer this reliably."
        ],
    )


async def _synth_refuse(
    question: str,
    record_facts: list[SourceCitation],
    guideline_evidence: list[SourceCitation],
) -> GroundedSummary:
    """Refusal path: fail closed with a safe, PHI-free reason (no fabrication)."""

    raise LLMError(
        "request refused before any clinical read (safety gate).",
        retriable=False,
    )


def _scenario(
    case: GoldenCase,
) -> tuple[list[LabReport | IntakeFacts], list[GuidelineEvidence], _Synth]:
    """Map a case to its (extracted docs, guideline evidence, stub synthesizer)."""

    inp = case.input
    if case.kind == "extraction":
        return _build_extracted(inp), [], _synth_grounded
    if case.kind == "evidence":
        return [], _evidence_for(case.query, inp.get("topic")), _synth_grounded
    if case.kind == "citation":
        extracted = _build_extracted(inp) if inp.get("doc") else []
        return extracted, _evidence_for(case.query, inp.get("topic")), _synth_with_ungrounded
    if case.kind == "refusal":
        return [], [], _synth_refuse
    # missing_data
    return [], [], _synth_caveats_only


# ---------------------------------------------------------------------------
# Case execution (real build_answer pipeline; logs captured for PHI scanning)
# ---------------------------------------------------------------------------


@dataclass
class CaseOutcome:
    """The measured result of running one case through the real pipeline."""

    case_id: str
    kind: str
    answer: W2Answer | None
    #: (model_cls, dumped_data) pairs to re-validate against their contracts.
    artifacts: list[tuple[type[BaseModel], dict[str, Any]]]
    refused: bool
    refusal_reason: str | None
    schema_error: str | None
    dropped_claims: int
    logs: list[str]


def _run_case(case: GoldenCase) -> CaseOutcome:
    """Run one case offline through :func:`build_answer`, capturing its logs."""

    buffer = io.StringIO()
    configure_logging(level="INFO", stream=buffer)
    log = get_logger("copilot.evals.w2_runner")

    answer: W2Answer | None = None
    refused = False
    refusal_reason: str | None = None
    schema_error: str | None = None
    artifacts: list[tuple[type[BaseModel], dict[str, Any]]] = []

    try:
        try:
            extracted, evidence, synth = _scenario(case)
        except ValidationError as exc:
            # Raw extraction did not satisfy its schema -> rejected at the gate.
            schema_error = type(exc).__name__
            extracted, evidence, synth = [], [], None  # type: ignore[assignment]

        if synth is not None:
            for model in extracted:
                artifacts.append((type(model), model.model_dump()))
            result = GraphResult(
                correlation_id="golden",
                patient_id="synthetic-patient",
                question=case.query,
                extracted=list(extracted),
                evidence=list(evidence),
                handoffs=[],
                done=True,
                steps=0,
            )
            try:
                answer = asyncio.run(build_answer(result, synthesize=synth))
                artifacts.append((W2Answer, answer.model_dump()))
            except LLMError as exc:
                refused = True
                refusal_reason = str(exc)

        log.info(
            "golden.case.ran",
            case_id=case.id,
            kind=case.kind,
            refused=refused,
            claims=len(answer.answer_claims) if answer is not None else 0,
        )
    finally:
        configure_logging()  # restore the default (stdout) logger

    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    return CaseOutcome(
        case_id=case.id,
        kind=case.kind,
        answer=answer,
        artifacts=artifacts,
        refused=refused,
        refusal_reason=refusal_reason,
        schema_error=schema_error,
        dropped_claims=_parse_dropped(lines),
        logs=lines,
    )


def _parse_dropped(lines: list[str]) -> int:
    """Read the gate's dropped-claim count out of the ``answer.assembled`` log."""

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "answer.assembled":
            return int(event.get("dropped", 0))
    return 0


# ---------------------------------------------------------------------------
# PHI log scanner (real inspection of emitted logs)
# ---------------------------------------------------------------------------

# High-signal PHI patterns. Chosen so ISO timestamps and uuid correlation ids in
# structured logs never false-positive (no bare YYYY-MM-DD date rule).
_PHI_PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b\d{3}-\d{3}-\d{4}\b"),
    "mrn": re.compile(r"\b(?:MRN[:#]?\s*\d+|FAKE-\d{3,})\b", re.IGNORECASE),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "dob_label": re.compile(r"\bDOB\b\s*[:=]"),
}

# Synthetic patient identifiers from the fixtures that must never reach a log.
_PHI_NAMES: frozenset[str] = frozenset({"Jordan Q. Testpatient", "Jordan Testpatient"})


def scan_for_phi(lines: list[str]) -> list[dict[str, Any]]:
    """Scan emitted log lines for PHI, returning one hit per match.

    Genuinely inspects each log line's text against the PHI pattern set and the
    known synthetic-patient names, so ``no_phi_in_logs`` reflects what was
    actually logged, not an assumption. Returns ``[]`` when the logs are clean.
    """

    hits: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        for name, pattern in _PHI_PATTERNS.items():
            for match in pattern.finditer(line):
                hits.append({"line": i, "pattern": name, "match": match.group(0)})
        for patient_name in _PHI_NAMES:
            if patient_name in line:
                hits.append({"line": i, "pattern": "patient_name", "match": patient_name})
    return hits


# ---------------------------------------------------------------------------
# Rubric evaluators (all boolean; deterministic)
# ---------------------------------------------------------------------------


def eval_schema_valid(outcome: CaseOutcome) -> bool:
    """True when every produced artifact validates against its Pydantic contract."""

    if outcome.schema_error is not None:
        return False
    try:
        for model_cls, data in outcome.artifacts:
            model_cls.model_validate(data)
    except ValidationError:
        return False
    return True


def eval_citation_present(outcome: CaseOutcome) -> bool:
    """True when no surfaced claim lacks a citation (vacuous if none surfaced)."""

    if outcome.answer is None:
        return True
    return all(len(claim.sources) >= 1 for claim in outcome.answer.answer_claims)


def eval_factually_consistent(outcome: CaseOutcome) -> bool:
    """True when every surfaced claim resolves to available evidence (gate held)."""

    if outcome.answer is None:
        return True
    available = {
        (c.source_type, c.source_id)
        for c in (*outcome.answer.record_facts, *outcome.answer.guideline_evidence)
    }
    for claim in outcome.answer.answer_claims:
        for source in claim.sources:
            if (source.resource_type, source.id) not in available:
                return False
    return True


def eval_safe_refusal(outcome: CaseOutcome) -> bool:
    """True when the case refused safely: a PHI-free reason and no surfaced answer."""

    if not outcome.refused or outcome.answer is not None:
        return False
    reason = outcome.refusal_reason or ""
    return bool(reason) and not scan_for_phi([reason])


def eval_no_phi_in_logs(outcome: CaseOutcome) -> bool:
    """True when the case's emitted logs contain no PHI (real scan)."""

    return not scan_for_phi(outcome.logs)


#: The rubric registry: category name -> boolean evaluator.
RUBRICS: dict[str, Callable[[CaseOutcome], bool]] = {
    "schema_valid": eval_schema_valid,
    "citation_present": eval_citation_present,
    "factually_consistent": eval_factually_consistent,
    "safe_refusal": eval_safe_refusal,
    "no_phi_in_logs": eval_no_phi_in_logs,
}


# ---------------------------------------------------------------------------
# Report contract (stable / machine-comparable for PRP-12)
# ---------------------------------------------------------------------------


class CaseResult(BaseModel):
    """Per-case scoring result: expected vs measured, and the pass verdict."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    expected: dict[str, bool]
    measured: dict[str, bool]
    passed: dict[str, bool]
    passed_all: bool
    dropped_claims: int = 0


class EvalReport(BaseModel):
    """The graded golden-set report — the committed, diff-able eval baseline.

    Serialised with sorted keys and no timestamps so PRP-12 can compare two
    runs field-for-field and flag a >5% per-category regression.
    """

    model_config = ConfigDict(extra="forbid")

    total: int
    counts_by_kind: dict[str, int]
    per_category_pass_rate: dict[str, float]
    overall_pass_rate: float
    cases: list[CaseResult]

    def to_json(self) -> str:
        """Render as stable JSON (sorted keys, trailing newline)."""

        return json.dumps(self.model_dump(), indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_golden(
    *,
    cases_dir: Path = CASES_DIR,
    write: bool = True,
    results_path: Path = RESULTS_PATH,
    push_langfuse: bool = False,
) -> EvalReport:
    """Run every golden case offline and score it against the five rubrics.

    Loads the repo cases, runs each through the real answer pipeline with a
    stubbed model, measures the five boolean rubrics, and computes per-category
    pass rates (measured == expected). Writes the committed ``results.json``
    unless ``write=False``; pushes to Langfuse only when ``push_langfuse`` and
    keys are configured.
    """

    cases = load_cases(cases_dir)

    results: list[CaseResult] = []
    cat_pass = dict.fromkeys(RUBRIC_NAMES, 0)

    for case in cases:
        outcome = _run_case(case)
        measured = {name: fn(outcome) for name, fn in RUBRICS.items()}
        passed = {name: measured[name] == case.expected[name] for name in RUBRIC_NAMES}
        for name in RUBRIC_NAMES:
            cat_pass[name] += int(passed[name])
        results.append(
            CaseResult(
                id=case.id,
                kind=case.kind,
                expected={name: case.expected[name] for name in RUBRIC_NAMES},
                measured=measured,
                passed=passed,
                passed_all=all(passed.values()),
                dropped_claims=outcome.dropped_claims,
            )
        )

    results.sort(key=lambda r: r.id)
    total = len(results)
    per_category = {
        name: round(cat_pass[name] / total, 4) if total else 0.0 for name in RUBRIC_NAMES
    }
    overall = round(sum(r.passed_all for r in results) / total, 4) if total else 0.0
    counts_by_kind = dict(sorted(Counter(c.kind for c in cases).items()))

    report = EvalReport(
        total=total,
        counts_by_kind=counts_by_kind,
        per_category_pass_rate=dict(sorted(per_category.items())),
        overall_pass_rate=overall,
        cases=results,
    )

    if write:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(report.to_json())

    if push_langfuse:
        _push_to_langfuse(report)

    return report


# ---------------------------------------------------------------------------
# Optional Langfuse publish (reuses the Week-1 dataset pattern; no-op offline)
# ---------------------------------------------------------------------------

#: Golden-set dataset, kept distinct from the Week-1 live-agent safety dataset.
GOLDEN_DATASET_NAME = "clinical-copilot-golden-set"


def _push_to_langfuse(report: EvalReport) -> None:
    """Publish per-case rubric scores to Langfuse, mirroring ``langfuse_eval``.

    Reuses the Week-1 dataset/observability plumbing (:func:`langfuse_enabled`,
    :func:`get_langfuse_client`, :func:`flush`) rather than re-implementing it,
    and is a no-op when Langfuse keys are absent — so the offline eval never
    needs a network.
    """

    from copilot.observability import flush, get_langfuse_client, langfuse_enabled

    if not langfuse_enabled():
        logger.info("golden.langfuse.skip", reason="langfuse not configured")
        return

    client = get_langfuse_client()
    try:
        client.create_dataset(
            name=GOLDEN_DATASET_NAME,
            description="Graded 50-case golden set (boolean rubrics) for the Week-2 flow.",
        )
    except Exception:
        pass  # already exists

    for case in report.cases:
        client.create_dataset_item(
            dataset_name=GOLDEN_DATASET_NAME,
            id=case.id,
            input={"kind": case.kind},
            expected_output=case.expected,
            metadata={"measured": case.measured, "passed": case.passed},
        )
    flush()


def main() -> None:
    report = run_golden(write=True)
    print(f"golden set: {report.total} cases across {report.counts_by_kind}")
    print("per-category pass rate:")
    for name, rate in report.per_category_pass_rate.items():
        print(f"  {name:<22} {rate:.4f}")
    print(f"overall pass rate: {report.overall_pass_rate:.4f}")
    print(f"wrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()
