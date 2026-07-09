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
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from copilot.llm.client import LLMClient, LLMError
from copilot.logging import get_logger
from copilot.observability import trace
from copilot.openemr.client import FhirClient
from copilot.openemr.deltas import get_deltas_since_last_visit
from copilot.openemr.panel import break_glass, is_patient_in_panel
from copilot.openemr.roles import (
    TokenSource,
    authorize_clinical_access,
    resolve_role,
)
from copilot.orchestrator.cache import Cache, TTLCache
from copilot.orchestrator.conversation import ConversationStore
from copilot.orchestrator.prewarm import cached_critical_set, critical_set_key
from copilot.schemas.clinical import CriticalSet, Deltas, PanelDecision
from copilot.schemas.conversation import ConversationTurn, GroundedAnswer
from copilot.schemas.output import Claim
from copilot.verification.gate import (
    VerifiedSummary,
    _is_grounded,
    _valid_refs,
    verify,
)

__all__ = [
    "PatientSummary",
    "FollowupResult",
    "Orchestrator",
    "HandRolledOrchestrator",
]

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


class FollowupResult(BaseModel):
    """The orchestrator's result for one answered follow-up turn (FR-7, UC-3).

    ``answer`` carries only the *grounded* claims — those citing records present
    in the conversation's retained :class:`CriticalSet`; ``dropped`` holds any
    claim removed for citing a record not in that set (the same M1-6 grounding
    contract the one-shot summary is held to). ``patient_id`` is the pinned
    patient the conversation resolved to — never re-selected from the question.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(description="The conversation this answer belongs to.")
    patient_id: str = Field(description="The pinned patient the conversation resolved to.")
    answer: GroundedAnswer = Field(description="The grounded, source-bound follow-up answer.")
    dropped: list[Claim] = Field(
        default_factory=list,
        description="Answer claims dropped for citing records not in the retained set.",
    )


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

    ``token_source`` vends the acting user's bearer token so the **role gate**
    (M2-3) can decide *who is asking* before any panel check or clinical read;
    when it is ``None`` (the M1 dev/test path) the role gate is skipped and the
    behaviour is the original gate → retrieve → synth → verify lifecycle.
    ``cache`` is the short-lived shared store (NFR-5) backing both cache-through
    critical-set reads (M2-4) and multi-turn conversation state (M2-2); it must
    be shared across requests so a follow-up resolves a conversation started by
    an earlier request. An in-memory :class:`TTLCache` is used when none is
    injected.
    """

    def __init__(
        self,
        *,
        fhir_client: FhirClient,
        llm_client: LLMClient | None = None,
        token_source: TokenSource | None = None,
        cache: Cache[Any] | None = None,
    ) -> None:
        self._fhir = fhir_client
        self._llm = llm_client or LLMClient()
        self._token_source = token_source
        self._cache: Cache[Any] = cache if cache is not None else TTLCache()
        self._conversations = ConversationStore(self._cache)

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
            # 0. Role gate first (M2-3) — *who* is asking? A non-clinical identity
            #    is refused before the panel check and before any clinical read,
            #    mirroring the out-of-panel short-circuit. Skipped when no acting
            #    identity is wired (the M1 dev/test path), so retrieval-only
            #    behaviour is unchanged.
            role_refusal = await self._role_gate(provider_id)
            if role_refusal is not None:
                span.update(
                    output={"authorized": False}, metadata={"refused": "role"}
                )
                return PatientSummary(
                    patient_id=patient_id,
                    provider_id=provider_id,
                    decision=role_refusal,
                )

            # 1. Panel gate — no clinical read happens for an out-of-panel patient.
            decision = await self._gate(patient_id, provider_id, break_glass_reason)
            if not decision.in_panel:
                span.update(output={"in_panel": False}, metadata={"refused": True})
                return PatientSummary(
                    patient_id=patient_id,
                    provider_id=provider_id,
                    decision=decision,
                )

            # 2. Parallel critical-set + deltas retrieval (FR-4). The critical set
            #    is read *through the cache* (M2-4): a prewarmed chart is served
            #    warm, a cold one is fetched live and populated for the next
            #    reader (and for this patient's follow-up turns).
            critical_set, deltas_result = await asyncio.gather(
                cached_critical_set(patient_id, client=self._fhir, cache=self._cache),
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

    async def _role_gate(self, provider_id: str) -> PanelDecision | None:
        """Resolve the acting user's role and refuse a non-clinical identity (M2-3).

        Returns a refusal :class:`PanelDecision` (``in_panel=False``) when the
        acting role is not authorized for clinical access — the refusal is logged
        by :func:`authorize_clinical_access` — so the request short-circuits to a
        refusal envelope with no retrieval, exactly like an out-of-panel patient.
        Returns ``None`` (allow) for an authorized physician role, or when no
        acting identity is wired (the dev/test path), leaving the M1 behaviour
        unchanged.
        """

        if self._token_source is None:
            return None

        role = await resolve_role(client=self._token_source)
        if authorize_clinical_access(role, user=provider_id):
            return None

        return PanelDecision(
            in_panel=False,
            reason=(
                f"Acting user's role ({role.value}) is not authorized for clinical "
                "chart access; access refused before any patient data was read."
            ),
        )

    async def start_conversation(
        self,
        patient_id: str,
        provider_id: str,
        *,
        break_glass_reason: str | None = None,
    ) -> tuple[PatientSummary, str | None]:
        """Run the gated summary and, if access is granted, pin a conversation.

        Returns the :class:`PatientSummary` envelope plus the new conversation id
        — or ``None`` for the id when access was refused (nothing is pinned for a
        refused request). The conversation references the patient's retained
        critical set by its cache key (M2-4), which the summary run has already
        warmed, so a later follow-up resolves the same records without a re-fetch.
        """

        result = await self.patient_summary(
            patient_id, provider_id, break_glass_reason=break_glass_reason
        )
        if result.refused:
            return result, None

        conversation_id = await self._conversations.start(
            patient_id, critical_set_key(patient_id)
        )
        return result, conversation_id

    async def answer_followup(
        self,
        conversation_id: str,
        question: str,
        provider_id: str,
    ) -> FollowupResult:
        """Answer a follow-up over a started conversation (FR-7, UC-3).

        Loads the pinned conversation (raising
        :class:`~copilot.orchestrator.conversation.ConversationNotFoundError` when
        it is unknown or expired), resolves the patient's retained critical set
        from the warm cache (re-fetching on a cold miss), and calls
        :meth:`LLMClient.answer_followup` with the retained turns so pronouns
        resolve to the pinned patient — the chart is never re-selected. The
        answer's claims are then held to the same M1-6 grounding gate as the
        one-shot summary (a claim citing a record absent from the retained set is
        dropped), and the user + assistant turns are appended.
        """

        with trace(
            "answer_followup",
            metadata={"conversation_id": conversation_id, "provider_id": provider_id},
        ) as span:
            state = await self._conversations.get(conversation_id)

            # Resolve the retained records: warm on a cache hit, live-fetched (and
            # re-warmed) if the entry has expired since the conversation started.
            critical_set = await self._cache.get(state.critical_set_ref)
            if critical_set is None:
                critical_set = await cached_critical_set(
                    state.patient_id, client=self._fhir, cache=self._cache
                )

            deltas = critical_set.deltas or Deltas()
            # The grounding ground-truth reads CriticalSet.deltas, so ensure the
            # deltas are attached before building the valid-source set.
            grounded_input = (
                critical_set
                if critical_set.deltas is not None
                else critical_set.model_copy(update={"deltas": deltas})
            )

            try:
                answer = await self._llm.answer_followup(
                    question, state.turns, critical_set, deltas
                )
            except LLMError:
                logger.warning(
                    "answer_followup.llm_unavailable", conversation_id=conversation_id
                )
                answer = GroundedAnswer(
                    answer=[],
                    caveats=[
                        "The follow-up could not be answered right now; please retry."
                    ],
                )

            # Grounding gate (FR-8/10): keep only claims whose sources are all in
            # the retained set; drop the rest rather than surface an invented cite.
            valid = _valid_refs(grounded_input)
            kept = [claim for claim in answer.answer if _is_grounded(claim, valid)]
            dropped = [claim for claim in answer.answer if not _is_grounded(claim, valid)]
            grounded_answer = answer.model_copy(update={"answer": kept})

            # Append the exchange; the patient stays pinned (append cannot change
            # it), so the conversation can never silently pivot charts.
            await self._conversations.append(
                conversation_id, ConversationTurn(role="user", text=question)
            )
            await self._conversations.append(
                conversation_id,
                ConversationTurn(role="assistant", text=_answer_text(grounded_answer)),
            )

            span.update(
                output={"kept": len(kept), "dropped": len(dropped)},
                metadata={
                    "kept": len(kept),
                    "dropped": len(dropped),
                    "turns": len(state.turns),
                },
            )
            return FollowupResult(
                conversation_id=conversation_id,
                patient_id=state.patient_id,
                answer=grounded_answer,
                dropped=dropped,
            )


def _answer_text(answer: GroundedAnswer) -> str:
    """Flatten a grounded answer into one turn's text (kept claims, then caveats).

    :class:`ConversationTurn` requires non-empty text; an answer with no grounded
    claims falls back to its caveats, and finally to a fixed placeholder, so a
    fully-dropped or empty answer still appends a valid assistant turn.
    """

    parts = [claim.text for claim in answer.answer] or list(answer.caveats)
    text = " ".join(part.strip() for part in parts if part.strip()).strip()
    return text or "No grounded answer was available for this question."


def _merge_missing(*groups: list[str]) -> list[str]:
    """Union the ``missing`` markers across sources, preserving first-seen order."""

    merged: dict[str, None] = {}
    for group in groups:
        for name in group:
            merged.setdefault(name, None)
    return list(merged)
