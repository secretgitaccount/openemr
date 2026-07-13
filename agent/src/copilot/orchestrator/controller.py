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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from copilot.config import get_settings
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
from copilot.openemr.retrieval import get_critical_set
from copilot.orchestrator.prewarm import cached_critical_set, critical_set_key
from copilot.orchestrator.summary_cache import CachedSummary, cache_key, summary_cache
from copilot.schemas.clinical import Deltas, PanelDecision, Problem
from copilot.schemas.conversation import ConversationTurn, GroundedAnswer
from copilot.schemas.output import Claim
from copilot.verification.gate import (
    VerifiedSummary,
    _is_grounded,
    _valid_refs,
    verify,
)
from copilot.verification.rules import RuleFlag

__all__ = [
    "PatientSummary",
    "FollowupResult",
    "Orchestrator",
    "HandRolledOrchestrator",
    "SummaryEvent",
    "RefusalEvent",
    "HeadlineEvent",
    "ClaimEvent",
    "FlagEvent",
    "CaveatEvent",
    "NoticeEvent",
    "DataAsOfEvent",
]

logger = get_logger(__name__)

# The ``missing`` marker used when the LLM synthesis step itself fails: the
# retrieval succeeded but no trustworthy summary could be produced (FR-11).
_SUMMARY_UNIT = "summary"

# Bump when the prompt or verification rules change, to invalidate every cached
# summary (the model is already in the version, so a model swap invalidates
# automatically). See copilot.orchestrator.summary_cache.
_SUMMARY_PROMPT_VERSION = "v1"


def _generation_version() -> str:
    """Cache-invalidation tag for generated summaries: model + prompt/rules version."""

    return f"{get_settings().anthropic_model}:{_SUMMARY_PROMPT_VERSION}"


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
    labs_omitted: int = Field(
        default=0,
        description="Lab records available but not analysed (bounded away); 0 when full labs analysed.",
    )
    problems: list[Problem] = Field(
        default_factory=list,
        description="The patient's active problem list (Conditions), rendered verbatim.",
    )
    generated_at: datetime | None = Field(
        default=None,
        description="When Claude generated this summary (may predate the request if served from cache).",
    )
    from_cache: bool = Field(
        default=False,
        description="True when the summary was served from the in-memory cache (chart unchanged).",
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


# ---------------------------------------------------------------------------
# Progressive stream events (M3-2) — each finalized stage, emitted as it lands
# ---------------------------------------------------------------------------
#
# The endpoint renders these to NDJSON as they arrive, so the response starts
# before the whole envelope is materialised (FR-12 incremental streaming). They
# carry only already-source-bound records — the endpoint layer owns the wire
# serialisation (``source_id`` rendering), keeping PHI shaping in one place.


@dataclass(frozen=True)
class RefusalEvent:
    """A gate refusal — the only event a refused request streams."""

    patient_id: str
    reason: str


@dataclass(frozen=True)
class HeadlineEvent:
    """The summary headline, emitted the moment the summary verifies."""

    text: str


@dataclass(frozen=True)
class ClaimEvent:
    """One grounded, cited claim (``must_know`` or ``what_changed``)."""

    kind: Literal["must_know", "what_changed"]
    claim: Claim


@dataclass(frozen=True)
class FlagEvent:
    """One deterministic safety-rule finding."""

    flag: RuleFlag


@dataclass(frozen=True)
class CaveatEvent:
    """One plain-language caveat / hedge."""

    text: str


@dataclass(frozen=True)
class NoticeEvent:
    """A "couldn't retrieve X" notice for an un-retrieved tier (FR-12)."""

    field: str


@dataclass(frozen=True)
class DataAsOfEvent:
    """The closing "data as of <ts>" stamp."""

    timestamp: datetime


#: The tagged union of everything :meth:`HandRolledOrchestrator.stream_patient_summary`
#: yields; the endpoint renders each variant to one NDJSON line.
SummaryEvent = (
    RefusalEvent
    | HeadlineEvent
    | ClaimEvent
    | FlagEvent
    | CaveatEvent
    | NoticeEvent
    | DataAsOfEvent
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

    def stream_patient_summary(
        self,
        patient_id: str,
        provider_id: str,
        *,
        break_glass_reason: str | None = None,
    ) -> AsyncIterator[SummaryEvent]:
        """Run the lifecycle and yield each stage's result as it finalizes."""
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
        full_labs: bool = False,
        force_regenerate: bool = False,
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
            # full_labs (opt-in "analyse everything") bypasses the cache and the
            # lab bound; the default path reads the bounded set through the cache.
            critical_set_coro = (
                get_critical_set(patient_id, client=self._fhir, full_labs=True)
                if full_labs
                else cached_critical_set(patient_id, client=self._fhir, cache=self._cache)
            )
            critical_set, deltas_result = await asyncio.gather(
                critical_set_coro,
                get_deltas_since_last_visit(patient_id, client=self._fhir),
            )
            deltas: Deltas = deltas_result.data
            missing = _merge_missing(critical_set.missing, deltas_result.missing)

            # Attach the deltas so the verification gate treats delta-sourced
            # records as valid grounding targets (its ground-truth set reads
            # CriticalSet.deltas).
            grounded_input = critical_set.model_copy(update={"deltas": deltas})

            # 3. Synthesis (FR-8) — served from the in-memory cache when the chart
            # is unchanged (skip the expensive Claude call). Freshness is keyed by
            # a hash of the retrieved data, not a clock. The panel + role gate and
            # audit already ran above, so this is a content cache only — it never
            # authorizes or bypasses the audit trail. See summary_cache.py.
            key = cache_key(patient_id, critical_set, deltas, _generation_version())
            cached = None if force_regenerate else summary_cache.get(key)
            if cached is not None:
                verified = cached.verified
                generated_at = cached.generated_at
                from_cache = True
                span.update(
                    output={"in_panel": True, "summarized": True, "cache": "hit"},
                    metadata={"missing": missing},
                )
            else:
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
                        problems=list(critical_set.problems),
                        generated_at=datetime.now(UTC),
                    )

                # 4. Verification (FR-10) — drop ungrounded claims, attach flags.
                verified = verify(summary, grounded_input)
                generated_at = datetime.now(UTC)
                summary_cache.put(key, CachedSummary(verified=verified, generated_at=generated_at))
                from_cache = False
                span.update(
                    output={"in_panel": True, "summarized": True, "cache": "miss"},
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
                labs_omitted=critical_set.labs_omitted,
                problems=list(critical_set.problems),
                generated_at=generated_at,
                from_cache=from_cache,
            )

    async def stream_patient_summary(
        self,
        patient_id: str,
        provider_id: str,
        *,
        break_glass_reason: str | None = None,
    ) -> AsyncIterator[SummaryEvent]:
        """Run the M1 lifecycle, emitting each stage's result as it finalizes (FR-12).

        The same gate → retrieve → synth → verify lifecycle as
        :meth:`patient_summary`, but instead of materialising the whole envelope
        before returning, this yields events progressively: a refused request
        streams a single :class:`RefusalEvent`; otherwise the headline is emitted
        the moment the summary verifies, then each grounded claim, each safety
        flag, each caveat, one :class:`NoticeEvent` per un-retrieved tier, and
        finally the :class:`DataAsOfEvent`. The borrowed-identity
        :class:`FhirClient` must stay open for the generator's whole life — the
        endpoint's request-scoped dependency keeps it so.
        """

        with trace(
            "patient_summary",
            metadata={"patient_id": patient_id, "provider_id": provider_id},
        ) as span:
            # 0. Role gate first (M2-3) — a non-clinical identity is refused
            #    before any read, exactly as in patient_summary.
            role_refusal = await self._role_gate(provider_id)
            if role_refusal is not None:
                span.update(output={"authorized": False}, metadata={"refused": "role"})
                yield RefusalEvent(patient_id=patient_id, reason=role_refusal.reason)
                return

            # 1. Panel gate — no clinical read for an out-of-panel patient.
            decision = await self._gate(patient_id, provider_id, break_glass_reason)
            if not decision.in_panel:
                span.update(output={"in_panel": False}, metadata={"refused": True})
                yield RefusalEvent(patient_id=patient_id, reason=decision.reason)
                return

            # 2. Parallel critical-set + deltas retrieval (FR-4), cache-through.
            critical_set, deltas_result = await asyncio.gather(
                cached_critical_set(patient_id, client=self._fhir, cache=self._cache),
                get_deltas_since_last_visit(patient_id, client=self._fhir),
            )
            deltas: Deltas = deltas_result.data
            missing = _merge_missing(critical_set.missing, deltas_result.missing)
            grounded_input = critical_set.model_copy(update={"deltas": deltas})

            # 3. Synthesis (FR-8) — degrade to notices rather than fabricate.
            try:
                summary = await self._llm.summarize(critical_set, deltas)
            except LLMError:
                logger.warning("patient_summary.llm_unavailable", patient_id=patient_id)
                missing = _merge_missing(missing, [_SUMMARY_UNIT])
                span.update(
                    output={"in_panel": True, "summarized": False},
                    metadata={"missing": missing},
                )
                for name in missing:
                    yield NoticeEvent(field=name)
                yield DataAsOfEvent(timestamp=critical_set.retrieved_at)
                return

            # 4. Verification (FR-10) — drop ungrounded claims, attach flags.
            verified = verify(summary, grounded_input)

            # 5. Emit each finalized piece progressively (not compute-then-emit):
            #    headline first, then claims, flags, caveats, notices, stamp.
            yield HeadlineEvent(text=verified.summary.headline)
            for claim in verified.summary.must_knows:
                yield ClaimEvent(kind="must_know", claim=claim)
            for claim in verified.summary.whats_changed:
                yield ClaimEvent(kind="what_changed", claim=claim)
            for flag in verified.flags:
                yield FlagEvent(flag=flag)
            for caveat in verified.summary.caveats:
                yield CaveatEvent(text=caveat)
            for name in missing:
                yield NoticeEvent(field=name)
            yield DataAsOfEvent(timestamp=critical_set.retrieved_at)

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
        full_labs: bool = False,
        force_regenerate: bool = False,
    ) -> tuple[PatientSummary, str | None]:
        """Run the gated summary and, if access is granted, pin a conversation.

        Returns the :class:`PatientSummary` envelope plus the new conversation id
        — or ``None`` for the id when access was refused (nothing is pinned for a
        refused request). The conversation references the patient's retained
        critical set by its cache key (M2-4), which the summary run has already
        warmed, so a later follow-up resolves the same records without a re-fetch.
        """

        result = await self.patient_summary(
            patient_id,
            provider_id,
            break_glass_reason=break_glass_reason,
            full_labs=full_labs,
            force_regenerate=force_regenerate,
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

            # The grounding ground-truth reads CriticalSet.deltas, so the retained
            # set must carry the deltas before the valid-source set is built. The
            # cached critical set holds only the plain tiers (deltas is None), so a
            # "what changed since last visit" follow-up would otherwise cite delta
            # records absent from the grounding set and be dropped (the M2-5 gap).
            # Recompute and attach them so those claims ground (M3-2).
            deltas = critical_set.deltas
            if deltas is None:
                deltas = await self._recompute_deltas(state.patient_id)
                critical_set = critical_set.model_copy(update={"deltas": deltas})
            grounded_input = critical_set

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

    async def _recompute_deltas(self, patient_id: str) -> Deltas:
        """Recompute the what-changed deltas for a follow-up's grounding set (M3-2).

        Called when the retained critical set has no deltas attached, so a
        "what changed since last visit" follow-up can ground against the
        delta-sourced records (new encounters, meds, problems, labs) rather than
        dropping them. Resilient by contract: any fetch failure degrades to an
        empty :class:`Deltas` (grounding simply admits no delta records) instead
        of sinking the follow-up.
        """

        try:
            result = await get_deltas_since_last_visit(patient_id, client=self._fhir)
        except Exception:
            logger.warning("answer_followup.deltas_unavailable", patient_id=patient_id)
            return Deltas()
        return result.data


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
