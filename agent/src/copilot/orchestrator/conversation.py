"""Patient-pinned, TTL-bounded multi-turn conversation state (FR-7, UC-3, NFR-5).

A conversation is started for exactly one patient and stays pinned to that chart
for its whole life. Follow-up turns are appended; they never re-select a patient,
so a conversation can never silently pivot to a different patient's record — a
safety property enforced by *shape*: :meth:`ConversationStore.append` has no way
to name a patient.

State lives in the short-lived shared :class:`~copilot.orchestrator.cache.Cache`
(NFR-5), not in a process global, so a follow-up that lands on a different replica
than the one that started the conversation still resolves. Each write refreshes
the TTL (a sliding window), so an abandoned conversation expires on its own rather
than accumulating.
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from copilot.orchestrator.cache import Cache
from copilot.schemas.conversation import ConversationTurn

__all__ = [
    "ConversationState",
    "ConversationStore",
    "ConversationNotFoundError",
    "DEFAULT_TTL_SECONDS",
]

#: Default conversation lifetime. Long enough to span a clinician's chart review,
#: short enough that abandoned state clears itself (NFR-5 "short-lived").
DEFAULT_TTL_SECONDS: float = 15 * 60

#: Cache-key prefix so conversation entries don't collide with other cache users.
_KEY_PREFIX = "conversation:"


class ConversationNotFoundError(LookupError):
    """The conversation does not exist, or its TTL has elapsed and it was dropped."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"no live conversation for id {conversation_id!r}")
        self.conversation_id = conversation_id


class ConversationState(BaseModel):
    """The retained state of one multi-turn conversation.

    Pins the conversation to a single ``patient_id`` and carries the turn history
    plus ``critical_set_ref`` — a reference to the patient's retrieved records
    (the prewarm/cache key, M2-4) rather than the records inline, so state stays
    small and the records are resolved once at answer time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str = Field(min_length=1, description="Opaque conversation id.")
    patient_id: str = Field(min_length=1, description="The pinned patient (FHIR id).")
    critical_set_ref: str = Field(
        min_length=1,
        description="Reference to the patient's retrieved records (e.g. a cache key).",
    )
    turns: list[ConversationTurn] = Field(
        default_factory=list,
        description="The conversation history, oldest first.",
    )


class ConversationStore:
    """Start, extend, and read patient-pinned conversations in a shared cache.

    All state lives in the injected :class:`Cache`, so the store itself is
    stateless and safe to reconstruct per request on any replica (NFR-5). The
    patient is fixed at :meth:`start`; :meth:`append` cannot change it.
    """

    def __init__(self, cache: Cache[ConversationState], *, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self._cache = cache
        self._ttl = ttl

    @staticmethod
    def _key(conversation_id: str) -> str:
        return f"{_KEY_PREFIX}{conversation_id}"

    async def start(self, patient_id: str, critical_set_ref: str) -> str:
        """Open a new conversation pinned to ``patient_id`` and return its id.

        The patient is fixed here for the conversation's whole life; no later call
        can re-select it.
        """

        conversation_id = str(uuid4())
        state = ConversationState(
            conversation_id=conversation_id,
            patient_id=patient_id,
            critical_set_ref=critical_set_ref,
        )
        await self._cache.set(self._key(conversation_id), state, self._ttl)
        return conversation_id

    async def append(self, conversation_id: str, turn: ConversationTurn) -> ConversationState:
        """Append ``turn`` to the conversation and return the updated state.

        The patient stays pinned: this reads the existing state, appends the turn,
        and writes it back with the same ``patient_id`` — there is no parameter to
        change the chart. Raises :class:`ConversationNotFoundError` if the
        conversation is unknown or expired.
        """

        state = await self.get(conversation_id)
        updated = state.model_copy(update={"turns": [*state.turns, turn]})
        await self._cache.set(self._key(conversation_id), updated, self._ttl)
        return updated

    async def get(self, conversation_id: str) -> ConversationState:
        """Return the live :class:`ConversationState`, refreshing nothing.

        Raises :class:`ConversationNotFoundError` when the conversation is unknown
        or its TTL has elapsed (the cache drops expired entries on read).
        """

        state = await self._cache.get(self._key(conversation_id))
        if state is None:
            raise ConversationNotFoundError(conversation_id)
        return state
