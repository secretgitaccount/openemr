"""The verification gate — the trust boundary between model and display (FR-8/9/10).

Two independent, deterministic (non-LLM) checks run here before anything the
model produced can be shown as fact:

1. **Grounding.** Every :class:`~copilot.schemas.output.Claim` must cite only
   source records that are actually present in the retrieved
   :class:`~copilot.schemas.clinical.CriticalSet`. A claim whose sources are not
   all in that ground-truth set is dropped rather than surfaced — the model
   cannot invent a citation.
2. **Domain rules.** Deterministic safety rules (see
   :mod:`copilot.verification.rules`) run over the retrieved data regardless of
   the model output, so an allergy contraindication in the data is flagged even
   when the model failed to mention it.

The result is recorded as a Langfuse verification event (PHI-scrubbed) so the
M3 dashboard can track pass/fail and dropped-claim counts.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from copilot.observability import record_verification, trace
from copilot.schemas.clinical import CriticalSet
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary
from copilot.verification.rules import RuleFlag, check_allergy_contraindications

__all__ = ["VerifiedSummary", "verify"]


class VerifiedSummary(BaseModel):
    """Outcome of the verification gate (FR-8/9/10).

    ``summary`` carries only the grounded claims; ``dropped`` holds the claims
    that were removed for citing records absent from the retrieved set; and
    ``flags`` holds the deterministic rule findings raised over the data.
    """

    model_config = ConfigDict(extra="forbid")

    summary: GroundedSummary = Field(description="The summary with only grounded claims retained.")
    dropped: list[Claim] = Field(
        default_factory=list,
        description="Claims dropped for citing records not in the retrieved set.",
    )
    flags: list[RuleFlag] = Field(
        default_factory=list,
        description="Deterministic safety-rule findings, independent of the model.",
    )


def _valid_refs(critical_set: CriticalSet) -> set[tuple[str, str]]:
    """Build the set of valid ``(resource_type, id)`` from the retrieved data.

    A claim may only cite a record that a retrieval tool actually returned, so
    grounding compares against the source pointers of every record in the
    :class:`CriticalSet` (including those nested inside ``deltas``). Timestamps
    are ignored — identity is the type/id pair.
    """

    refs: set[tuple[str, str]] = set()

    def add(source: SourceRef) -> None:
        refs.add((source.resource_type, source.id))

    for med in critical_set.medications:
        add(med.source)
    for allergy in critical_set.allergies:
        add(allergy.source)
    for lab in critical_set.labs:
        add(lab.source)
    for problem in critical_set.problems:
        add(problem.source)

    deltas = critical_set.deltas
    if deltas is not None:
        for med in (*deltas.new_meds, *deltas.stopped_meds):
            add(med.source)
        for problem in deltas.new_problems:
            add(problem.source)
        for lab in deltas.new_labs:
            add(lab.source)
        for encounter in deltas.new_encounters:
            add(encounter.source)

    return refs


def _is_grounded(claim: Claim, valid: set[tuple[str, str]]) -> bool:
    """True when the claim cites at least one source and **all** are valid."""

    if not claim.sources:
        return False
    return all((s.resource_type, s.id) in valid for s in claim.sources)


def verify(summary: GroundedSummary, critical_set: CriticalSet) -> VerifiedSummary:
    """Apply the grounding gate and deterministic rules (FR-8/9/10).

    Keeps only claims whose sources are all present in ``critical_set``, moving
    the rest to ``dropped``; runs the domain rules over the retrieved data; and
    records a PHI-scrubbed verification event with pass/fail and counts.
    """

    with trace("verify", metadata={"resource_type": "GroundedSummary"}) as span:
        valid = _valid_refs(critical_set)

        kept_must: list[Claim] = []
        kept_changed: list[Claim] = []
        dropped: list[Claim] = []

        for claim in summary.must_knows:
            (kept_must if _is_grounded(claim, valid) else dropped).append(claim)
        for claim in summary.whats_changed:
            (kept_changed if _is_grounded(claim, valid) else dropped).append(claim)

        grounded_summary = summary.model_copy(
            update={"must_knows": kept_must, "whats_changed": kept_changed}
        )

        flags = check_allergy_contraindications(critical_set)

        passed = not dropped
        counts = {
            "kept": len(kept_must) + len(kept_changed),
            "dropped": len(dropped),
            "flags": len(flags),
        }
        span.update(output=counts, metadata=counts)
        record_verification(passed, metadata=counts)

        return VerifiedSummary(summary=grounded_summary, dropped=dropped, flags=flags)
