"""Multi-turn conversation contracts (FR-7, UC-3).

A conversation retains the current-patient context across turns so a follow-up
like "what are *her* allergies?" resolves against the patient already in context.
These are the source-of-truth shapes for that exchange:

- :class:`ConversationTurn` — one line of the dialogue (user or assistant).
- :class:`GroundedAnswer` — the model's follow-up answer, still source-bound:
  every factual line is a :class:`~copilot.schemas.output.Claim` carrying the
  records that ground it, exactly as the one-shot summary is (FR-8). A follow-up
  is not an excuse to fabricate — it grounds against the *retained* records.

Design notes mirror `schemas/core.py`: pydantic v2, ``frozen`` value objects,
``extra="forbid"``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from copilot.schemas.output import Claim

__all__ = ["ConversationTurn", "GroundedAnswer"]


class ConversationTurn(BaseModel):
    """A single line of a multi-turn conversation (FR-7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"] = Field(description="Who produced this turn.")
    text: str = Field(min_length=1, description="The turn's natural-language text.")


class GroundedAnswer(BaseModel):
    """A source-bound answer to a follow-up question (FR-7, FR-8).

    The follow-up answer binds to the same grounding contract as the one-shot
    summary: `answer` is a list of :class:`Claim`, each carrying the source
    records it is drawn from. `caveats` are plain-language hedges (missing data,
    stale timestamps) and are intentionally not grounded claims.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: list[Claim] = Field(
        default_factory=list,
        description="The grounded facts answering the follow-up.",
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Explicit limitations / hedges (not grounded claims).",
    )
