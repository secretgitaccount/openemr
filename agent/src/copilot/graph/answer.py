"""Assemble the final grounded W2 answer + citation contract (PRP-10, FR-6).

This is the last step of the Week-2 flow: it turns the inspectable PRP-09
:class:`~copilot.graph.state.GraphResult` into a :class:`W2Answer` that a
clinician can trust. Three invariants shape it:

* **Record facts and guideline evidence are separate.** Facts extracted from the
  patient's own documents / OpenEMR records (:class:`SourceCitation` with a
  ``lab_pdf`` / ``intake_form`` / ``fhir`` source) live in ``record_facts``;
  public guideline passages retrieved by RAG live in ``guideline_evidence``. The
  two are **never merged** — the whole point of FR-6 is that a reader can always
  tell "what this patient's chart says" apart from "what the guideline says".
* **Grounding is enforced by the Week-1 gate, not by prompting.** The claims the
  LLM synthesizes are run through :func:`copilot.verification.gate.verify`; any
  claim whose citations are not all present in the available evidence is
  **dropped** rather than surfaced. A hallucinated value therefore never reaches
  the answer — it is silently removed and the safe, grounded remainder stands.
* **Every surfaced claim carries a citation.** After the gate, each claim in
  ``answer_claims`` cites at least one source that maps back to a
  :class:`SourceCitation` in ``record_facts`` or ``guideline_evidence``.

The synthesis LLM step is **injectable** (``synthesize``) so tests drive the
whole assembly with a stub — no Anthropic key, no network. The default binds the
Week-1 Anthropic client's ``messages.parse`` to the ``GroundedSummary`` schema
(the same grounding-by-shape pattern), and is only exercised at the LIVE smoke
in PRP-14.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from copilot.documents.ingest import IngestResult
from copilot.documents.schemas import (
    CitedList,
    CitedText,
    IntakeFacts,
    LabReport,
    SourceCitation,
)
from copilot.graph.state import GraphResult, Handoff
from copilot.llm.client import LLMClient, LLMError
from copilot.logging import CORRELATION_ID_HEADER, current_correlation_id, get_logger
from copilot.observability import trace
from copilot.rag.retrieve import GuidelineEvidence
from copilot.schemas.clinical import CriticalSet, Problem
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary
from copilot.verification.gate import verify

__all__ = ["W2Answer", "AnswerSynthesizer", "build_answer"]

logger = get_logger(__name__)

#: Output-token cap for the synthesis call (mirrors the Week-1 client headroom).
_MAX_TOKENS = 16000

#: The synthesis step: given the question and the two separated evidence lists,
#: produce a (raw, pre-verification) :class:`GroundedSummary` whose claims cite
#: the supplied evidence. Injectable so tests stub it — no key, no network.
AnswerSynthesizer = Callable[
    [str, list[SourceCitation], list[SourceCitation]], Awaitable[GroundedSummary]
]


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class W2Answer(BaseModel):
    """The final grounded answer for one Week-2 question (PRP-10, FR-6).

    ``record_facts`` (from the patient's documents / OpenEMR records) and
    ``guideline_evidence`` (from the RAG corpus) are deliberately distinct
    fields, never merged, so a reader can always separate chart facts from public
    guidance. Every claim in ``answer_claims`` survived the Week-1 verification
    gate, so it carries at least one citation that resolves to one of those two
    evidence lists. ``handoffs`` is the inspectable PRP-09 routing log. Frozen +
    ``extra="forbid"`` like every other contract in the codebase.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str = Field(min_length=1, description="One-line answer headline.")
    answer_claims: list[Claim] = Field(
        default_factory=list,
        description="Grounded claims (each carries >=1 citation; ungrounded ones were dropped).",
    )
    record_facts: list[SourceCitation] = Field(
        default_factory=list,
        description="Patient-record citations (documents / OpenEMR); separate from guidelines.",
    )
    guideline_evidence: list[SourceCitation] = Field(
        default_factory=list,
        description="Guideline-corpus citations from RAG; separate from record facts.",
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Plain-language limitations / hedges (not grounded claims).",
    )
    handoffs: list[Handoff] = Field(
        default_factory=list,
        description="The inspectable PRP-09 supervisor routing log for this run.",
    )


# ---------------------------------------------------------------------------
# Evidence extraction — record facts vs guideline evidence (kept separate)
# ---------------------------------------------------------------------------


def _dedup(citations: list[SourceCitation]) -> list[SourceCitation]:
    """Drop exact-duplicate citations, preserving first-seen order."""

    seen: set[SourceCitation] = set()
    out: list[SourceCitation] = []
    for c in citations:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _lab_citations(report: LabReport) -> list[SourceCitation]:
    """Every grounding citation in an extracted lab report.

    Each observation's citation is enriched so its ``quote_or_value`` names the
    test, value, unit and (when concerning) the abnormal flag — e.g.
    ``"Glucose: 168 mg/dL [high]"``. The synthesis LLM and the UI caption need
    that label, not a bare number; the bbox in ``field_or_chunk_id`` is left
    untouched so the click-to-source overlay stays exact, and grounding still
    keys on ``(source_type, source_id)``.
    """

    cited: list[SourceCitation] = [report.source]
    for obs in report.observations:
        value = obs.value if obs.value is not None else "not reported"
        label = f"{obs.test_name}: {value}"
        if obs.value is not None and obs.unit:
            label += f" {obs.unit}"
        if obs.abnormal_flag in ("high", "low", "critical"):
            label += f" [{obs.abnormal_flag}]"
        cited.append(obs.citation.model_copy(update={"quote_or_value": label}))
    return cited


def _intake_citations(facts: IntakeFacts) -> list[SourceCitation]:
    """Every grounding citation in an extracted intake form."""

    cited: list[SourceCitation] = [facts.source, facts.demographics.citation]
    for value in (
        facts.chief_concern,
        facts.current_medications,
        facts.allergies,
        facts.family_history,
    ):
        if isinstance(value, (CitedText, CitedList)):
            cited.append(value.citation)
    return cited


def _record_facts(extracted: list[object]) -> list[SourceCitation]:
    """Collect record-fact citations from the graph's extracted-document results.

    Handles the PRP-06 :class:`IngestResult` (unwrapping its validated
    ``LabReport`` / ``IntakeFacts``) as well as a bare extracted model, so both
    the endpoint's real worker output and a test's direct fixtures work. Anything
    unrecognised is skipped rather than guessed at.
    """

    cited: list[SourceCitation] = []
    for item in extracted:
        model: object = item.extracted if isinstance(item, IngestResult) else item
        if isinstance(model, LabReport):
            cited.extend(_lab_citations(model))
        elif isinstance(model, IntakeFacts):
            cited.extend(_intake_citations(model))
    return _dedup(cited)


def _guideline_evidence(evidence: list[object]) -> list[SourceCitation]:
    """Turn PRP-08 :class:`GuidelineEvidence` into citable guideline pointers.

    Each retrieved chunk becomes a ``guideline``-typed :class:`SourceCitation`
    naming the chunk id, its section, and the passage text as the grounding
    evidence — kept strictly out of ``record_facts``.
    """

    cited: list[SourceCitation] = []
    for ev in evidence:
        if not isinstance(ev, GuidelineEvidence):
            continue
        chunk = ev.chunk
        cited.append(
            SourceCitation(
                source_type="guideline",
                source_id=chunk.chunk_id,
                page_or_section=chunk.section,
                field_or_chunk_id=chunk.chunk_id,
                quote_or_value=chunk.text,
            )
        )
    return _dedup(cited)


# ---------------------------------------------------------------------------
# Grounding — reuse the Week-1 verification gate to drop ungrounded claims
# ---------------------------------------------------------------------------


def _grounding_set(
    record_facts: list[SourceCitation], guideline_evidence: list[SourceCitation]
) -> CriticalSet:
    """Build the ground-truth set the Week-1 gate checks claims against.

    The gate (:func:`copilot.verification.gate.verify`) grounds a claim by
    comparing its ``(resource_type, id)`` source pointers against the records in a
    :class:`CriticalSet`. We adapt every available citation — record facts *and*
    guideline passages — into that set (one placeholder ``Problem`` per distinct
    citation, keyed by ``source_type`` / ``source_id``) so a claim survives the
    gate iff **all** its citations point at evidence we actually have.
    """

    seen: set[tuple[str, str]] = set()
    problems: list[Problem] = []
    for c in (*record_facts, *guideline_evidence):
        key = (c.source_type, c.source_id)
        if key in seen:
            continue
        seen.add(key)
        problems.append(
            Problem(
                id=c.source_id,
                name="grounding-evidence",
                source=SourceRef(resource_type=c.source_type, id=c.source_id),
            )
        )
    return CriticalSet(problems=problems)


# ---------------------------------------------------------------------------
# Default synthesis — Week-1 Anthropic client + messages.parse (stubbed in tests)
# ---------------------------------------------------------------------------


_ANSWER_SYSTEM_PROMPT = """\
You are a clinical co-pilot answering one question about a single patient. You are \
given, as JSON: the `question`, the patient's own `record_facts` (extracted from \
their documents / chart), and public `guideline_evidence` retrieved from a \
clinical-guideline corpus. Each evidence item carries a `resource_type`, an `id`, \
and the verbatim `evidence` text it was drawn from.

Produce a grounded summary. Hard rules:
- Every claim MUST carry, in its `sources`, the `resource_type` and `id` of each \
evidence item it draws from, copied verbatim from the input. If you cannot cite \
it, do not say it.
- NEVER assert a value or fact that is not backed by a supplied evidence item. Do \
not infer, extrapolate, or add general medical knowledge as this patient's data.
- If the evidence does not contain what was asked, say so in `caveats` rather than \
guessing. Keep record facts and guideline evidence distinct in your wording.

`caveats` are plain-language limitations and are NOT grounded claims — do not \
attach sources to them. Be concise and specific."""


def _synthesis_payload(
    question: str,
    record_facts: list[SourceCitation],
    guideline_evidence: list[SourceCitation],
) -> str:
    """Serialise the minimum-necessary synthesis payload (citable pointers only)."""

    def cite(c: SourceCitation) -> dict[str, str | None]:
        return {
            "resource_type": c.source_type,
            "id": c.source_id,
            "locator": c.page_or_section or c.field_or_chunk_id,
            "evidence": c.quote_or_value,
        }

    data = {
        "question": question,
        "record_facts": [cite(c) for c in record_facts],
        "guideline_evidence": [cite(c) for c in guideline_evidence],
    }
    return json.dumps(data, separators=(",", ":"))


async def _llm_synthesize(
    question: str,
    record_facts: list[SourceCitation],
    guideline_evidence: list[SourceCitation],
    *,
    llm: LLMClient | None = None,
) -> GroundedSummary:
    """Synthesize the raw answer via the Week-1 Anthropic client (LIVE path).

    Reuses :class:`LLMClient` for lazy client construction, key validation, and
    the target model, then binds ``messages.parse`` to the ``GroundedSummary``
    schema (grounding by shape). Never invoked in the build/unit tests — those
    inject a stub :data:`AnswerSynthesizer` — so no key is needed to test, per
    the PRP's key boundary. A refusal or unparseable response surfaces a typed
    :class:`LLMError` rather than a fabricated answer (FR-11).
    """

    llm = llm or LLMClient()
    # Reuse the Week-1 client's lazy construction + key validation + target model
    # (its package-internal accessors) rather than duplicating that wiring here.
    client = llm._anthropic()
    model = llm._model
    payload = _synthesis_payload(question, record_facts, guideline_evidence)
    cid = current_correlation_id()
    extra_headers = {CORRELATION_ID_HEADER: cid} if cid else None

    with trace("answer.synthesize", as_type="generation", metadata={"model": model}) as span:
        message = await client.messages.parse(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=_ANSWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": payload}],
            output_format=GroundedSummary,
            extra_headers=extra_headers,
        )
        if message.stop_reason == "refusal":
            logger.warning("answer.synthesize.refusal", model=model)
            raise LLMError(
                "the model refused to answer; surfacing rather than fabricating.",
                retriable=False,
            )
        summary = message.parsed_output
        if summary is None:
            logger.warning("answer.synthesize.unparseable", model=model)
            raise LLMError("the model returned no parseable GroundedSummary.", retriable=False)
        span.update(metadata={"claims": len(summary.must_knows) + len(summary.whats_changed)})
        return summary


# ---------------------------------------------------------------------------
# Public assembly
# ---------------------------------------------------------------------------


async def build_answer(
    result: GraphResult,
    *,
    synthesize: AnswerSynthesizer | None = None,
) -> W2Answer:
    """Assemble a verified :class:`W2Answer` from a PRP-09 :class:`GraphResult`.

    Separates the record-fact citations from the guideline-evidence citations,
    synthesizes candidate claims (via the injectable ``synthesize`` step), and
    runs every claim through the Week-1 verification gate so any ungrounded or
    hallucinated claim is **dropped** before it can be surfaced. The returned
    answer carries only grounded claims — each with a citation resolvable to one
    of the two evidence lists — plus the caveats and the inspectable handoff log.
    """

    synthesize = synthesize or _llm_synthesize

    record_facts = _record_facts(result.extracted)
    guideline_evidence = _guideline_evidence(result.evidence)

    raw = await synthesize(result.question, record_facts, guideline_evidence)

    grounding = _grounding_set(record_facts, guideline_evidence)
    verified = verify(raw, grounding)

    answer_claims = [*verified.summary.must_knows, *verified.summary.whats_changed]
    logger.info(
        "answer.assembled",
        record_facts=len(record_facts),
        guideline_evidence=len(guideline_evidence),
        claims=len(answer_claims),
        dropped=len(verified.dropped),
    )

    return W2Answer(
        headline=verified.summary.headline,
        answer_claims=answer_claims,
        record_facts=record_facts,
        guideline_evidence=guideline_evidence,
        caveats=list(verified.summary.caveats),
        handoffs=list(result.handoffs),
    )
