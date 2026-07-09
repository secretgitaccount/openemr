"""The request-lifecycle orchestrator — the M1 walking skeleton, end to end.

:class:`HandRolledOrchestrator` runs the full patient-summary lifecycle behind a
single swappable :class:`Orchestrator` interface (PRD §12):

1. **Gate first (FR-2).** :func:`~copilot.openemr.panel.is_patient_in_panel`
   decides access *before* any clinical read happens. An out-of-panel patient
   with no break-glass override stops here: the refusal is already audited by
   the gate (M1-2), and a refusal :class:`PatientSummary` is returned without a
   single record being fetched.
2. **Parallel retrieval (FR-4).** The critical "must-know" set
   (:func:`~copilot.openemr.retrieval.get_critical_set`) and the "what changed
   since last visit" deltas
   (:func:`~copilot.openemr.deltas.get_deltas_since_last_visit`) are fetched
   concurrently. Either degrading to a partial result is folded into
   :attr:`PatientSummary.missing` rather than sinking the request (FR-11).
3. **Synthesis (FR-8).** :meth:`LLMClient.summarize` turns the retrieved,
   source-bound records into a :class:`GroundedSummary` whose every claim cites
   real records.
4. **Verification (FR-10).** :func:`~copilot.verification.gate.verify` drops any
   claim citing a record absent from the retrieved set and attaches the
   deterministic rule flags — the trust boundary between model and display.
5. **Envelope.** The :class:`VerifiedSummary` is returned alongside the
   ``missing`` tiers and the data timestamp the endpoint needs for the FR-12
   "couldn't retrieve X" / "data as of <ts>" notices.

The whole flow runs under one correlation id (bound per request by
``copilot.middleware``) and a parent ``patient_summary`` trace span; PHI never
reaches a trace (payloads are scrubbed by ``copilot.observability``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from copilot.llm.client import LLMClient, LLMError
from copilot.logging import get_logger
from copilot.observability import trace
from copilot.openemr.client import FhirClient
from copilot.openemr.deltas import get_deltas_since_last_visit
from copilot.openemr.panel import break_glass, is_patient_in_panel
from copilot.openemr.retrieval import get_critical_set
from copilot.schemas.clinical import CriticalSet, Deltas, PanelDecision
from copilot.verification.gate import VerifiedSummary, verify

__all__ = ["PatientSummary", "Orchestrator", "HandRolledOrchestrator"]

logger = get_logger(__name__)

# The ``missing`` marker used when the LLM synthesis step itself fails: the
# retrieval succeeded but no trustworthy summary could be produced (FR-11).
_SUMMARY_UNIT = "summary"


class PatientSummary(BaseModel):
    """The orchestrator's result envelope for one patient-summary request.

    Carries everything the streamed endpoint renders: the panel decision, the
    verified (grounded + flagged) summary when access was granted, the tiers
    that could not be retrieved (``missing`` — the FR-12 "couldn't retrieve X"
    notices), and the data timestamp for the "data as of <ts>" line.

    A refusal envelope has ``in_panel=False`` and ``verified=None``: no
    retrieval ran, so there is nothing to summarise.
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: str = Field(description="The FHIR Patient id the summary is for.")
    provider_id: str = Field(description="The provider whose panel gated access.")
    decision: PanelDecision = Field(description="The panel-gate decision (FR-2).")
    verified: VerifiedSummary | None = Field(
        default=None,
        description="Grounded, verified summary; None when access was refused.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="Tiers that could not be retrieved (FR-12 notices).",
    )
    data_as_of: datetime | None = Field(
        default=None,
        description="When the retrieved data was assembled; None on refusal.",
    )

    @property
    def refused(self) -> bool:
        """True when the patient was out of panel and access was refused."""

        return not self.decision.in_panel


class Orchestrator(Protocol):
    """The swappable orchestration interface (PRD §12).

    A single method runs the whole request lifecycle and returns the
    :class:`PatientSummary` envelope. Implementations may be hand-rolled (M1) or
    framework-backed later, without any change to the endpoint that calls them.
    """

    async def patient_summary(
        self,
        patient_id: str,
        provider_id: str,
        *,
        break_glass_reason: str | None = None,
    ) -> PatientSummary:
        """Run the full lifecycle for one patient and return the envelope."""
        ...


class HandRolledOrchestrator:
    """The M1 hand-rolled :class:`Orchestrator`: gate → retrieve → synth → verify.

    ``fhir_client`` is the borrowed-identity :class:`FhirClient` every read goes
    through (FR-3). ``llm_client`` is injectable so tests can supply a fake with
    a ``summarize`` coroutine; otherwise a real :class:`LLMClient` is built
    lazily (its Anthropic client — and thus the API-key requirement — is only
    constructed on the first live call).
    """

    def __init__(
        self,
        *,
        fhir_client: FhirClient,
        llm_client: LLMClient | None = None,
    ) -> None:
        self._fhir = fhir_client
        self._llm = llm_client or LLMClient()

    async def patient_summary(
        self,
        patient_id: str,
        provider_id: str,
        *,
        break_glass_reason: str | None = None,
    ) -> PatientSummary:
        """Run gate → parallel retrieval → synthesis → verification (M1 lifecycle).

        Gates first (FR-2): an out-of-panel patient with no break-glass override
        short-circuits to a refusal envelope *before* any clinical retrieval, and
        the refusal is audited by the gate. Otherwise the critical set and deltas
        are fetched in parallel (FR-4), summarised (FR-8), and verified (FR-10);
        any retrieval or synthesis degradation is folded into ``missing`` (FR-11)
        rather than failing the request.
        """

        with trace(
            "patient_summary",
            metadata={"patient_id": patient_id, "provider_id": provider_id},
        ) as span:
            # 1. Gate first — no clinical read happens for an out-of-panel patient.
            decision = await self._gate(patient_id, provider_id, break_glass_reason)
            if not decision.in_panel:
                span.update(output={"in_panel": False}, metadata={"refused": True})
                return PatientSummary(
                    patient_id=patient_id,
                    provider_id=provider_id,
                    decision=decision,
                )

            # 2. Parallel critical-set + deltas retrieval (FR-4).
            critical_set, deltas_result = await asyncio.gather(
                get_critical_set(patient_id, client=self._fhir),
                get_deltas_since_last_visit(patient_id, client=self._fhir),
            )
            deltas: Deltas = deltas_result.data
            missing = _merge_missing(critical_set.missing, deltas_result.missing)

            # Attach the deltas so the verification gate treats delta-sourced
            # records as valid grounding targets (its ground-truth set reads
            # CriticalSet.deltas).
            grounded_input = critical_set.model_copy(update={"deltas": deltas})

            # 3. Synthesis (FR-8) — degrade to no summary rather than fabricate.
            try:
                summary = await self._llm.summarize(critical_set, deltas)
            except LLMError:
                logger.warning("patient_summary.llm_unavailable", patient_id=patient_id)
                missing = _merge_missing(missing, [_SUMMARY_UNIT])
                span.update(
                    output={"in_panel": True, "summarized": False},
                    metadata={"missing": missing},
                )
                return PatientSummary(
                    patient_id=patient_id,
                    provider_id=provider_id,
                    decision=decision,
                    missing=missing,
                    data_as_of=critical_set.retrieved_at,
                )

            # 4. Verification (FR-10) — drop ungrounded claims, attach rule flags.
            verified = verify(summary, grounded_input)

            span.update(
                output={"in_panel": True, "summarized": True},
                metadata={
                    "must_knows": len(verified.summary.must_knows),
                    "whats_changed": len(verified.summary.whats_changed),
                    "dropped": len(verified.dropped),
                    "flags": len(verified.flags),
                    "missing": missing,
                },
            )
            return PatientSummary(
                patient_id=patient_id,
                provider_id=provider_id,
                decision=decision,
                verified=verified,
                missing=missing,
                data_as_of=critical_set.retrieved_at,
            )

    async def _gate(
        self,
        patient_id: str,
        provider_id: str,
        break_glass_reason: str | None,
    ) -> PanelDecision:
        """Resolve the panel decision, honouring an explicit break-glass override.

        With a break-glass reason the access is granted and logged as an
        override (M1-2 audit); otherwise the normal schedule/encounter panel
        check runs and audits its own refusal.
        """

        if break_glass_reason is not None:
            result = await break_glass(
                patient_id, provider_id, break_glass_reason, client=self._fhir
            )
        else:
            result = await is_patient_in_panel(
                patient_id, provider_id, client=self._fhir
            )
        return result.data


def _merge_missing(*groups: list[str]) -> list[str]:
    """Union the ``missing`` markers across sources, preserving first-seen order."""

    merged: dict[str, None] = {}
    for group in groups:
        for name in group:
            merged.setdefault(name, None)
    return list(merged)
