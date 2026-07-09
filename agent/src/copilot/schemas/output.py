"""Source-bound structured-output contracts (FR-8).

`GroundedSummary` is the schema the model's structured output binds to (M1-5),
and `Claim` enforces grounding by *shape*: a factual statement must carry the
`SourceRef`s that back it. A claim with no sources is structurally
constructible but not a grounded fact — verification drops it in M1-6.

Design notes mirror `schemas/core.py`: pydantic v2, `frozen` value objects,
`extra="forbid"`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from copilot.schemas.core import SourceRef

__all__ = ["Claim", "GroundedSummary"]


class Claim(BaseModel):
    """A single statement plus the source records that ground it (FR-8).

    An empty `sources` list is allowed by construction but means the claim is
    ungrounded: `is_grounded` is False and downstream verification (M1-6) drops
    it rather than surfacing an unsupported fact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, description="The claim's natural-language text.")
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="Source records grounding the claim; empty means ungrounded.",
    )

    @property
    def is_grounded(self) -> bool:
        """True when the claim carries at least one grounding source."""
        return len(self.sources) > 0


class GroundedSummary(BaseModel):
    """The clinician-facing summary the model's structured output binds to (M1-5).

    Every factual line is a `Claim` so grounding is enforced by output shape.
    `caveats` are explicit hedges / limitations and are intentionally plain
    strings, not grounded claims.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    headline: str = Field(min_length=1, description="One-line summary headline.")
    must_knows: list[Claim] = Field(
        default_factory=list,
        description="Highest-priority grounded facts.",
    )
    whats_changed: list[Claim] = Field(
        default_factory=list,
        description="Grounded facts describing what changed since the last visit.",
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Explicit limitations / hedges (not grounded claims).",
    )
