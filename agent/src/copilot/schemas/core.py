"""Canonical cross-cutting Pydantic contracts (PRD NFR-3).

These models are the **source of truth** for tool I/O and shared value types.
Retrieval tools return them, the model's structured output binds to them, and
malformed data is rejected here at the schema layer rather than downstream.

Design notes:
- Pydantic v2 throughout.
- Value objects (`SourceRef`, `TokenResponse`, `AgentError`) are `frozen` so
  they behave as immutable, hashable identities once constructed.
- `extra="forbid"` everywhere: an unexpected key is a contract violation, not
  something to silently ignore.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SourceRef", "TokenResponse", "AgentError", "ToolResult"]


class SourceRef(BaseModel):
    """A pointer to a source record that grounds a clinical claim (FR-8).

    Every clinical statement the agent surfaces must carry one of these so
    grounding is enforced by output *shape*, not by prompting. It names the
    FHIR resource type, the record id, and (when known) the record's clinical
    timestamp.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_type: str = Field(min_length=1, description="FHIR resource type, e.g. 'Observation'.")
    id: str = Field(min_length=1, description="Record id within that resource type.")
    timestamp: datetime | None = Field(
        default=None,
        description="Clinical timestamp of the record, if known.",
    )


class TokenResponse(BaseModel):
    """OAuth2 token endpoint response (consumed by the OpenEMR client, M0-4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str = Field(min_length=1)
    token_type: str = Field(min_length=1)
    expires_in: int = Field(gt=0, description="Access-token lifetime in seconds.")
    refresh_token: str | None = None
    scope: str = Field(description="Space-delimited granted scopes.")


class AgentError(BaseModel):
    """Structured error surfaced across tool / orchestrator boundaries.

    `retriable` lets callers distinguish a transient failure (retry may help)
    from a permanent one (retrying is pointless).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, description="Stable, machine-readable error code.")
    message: str = Field(min_length=1, description="Human-readable description.")
    retriable: bool = Field(description="Whether retrying the operation may succeed.")


T = TypeVar("T")


class ToolResult(BaseModel, Generic[T]):
    """Base envelope for every retrieval tool's output (FR-8, FR-11).

    Carries the payload alongside the grounding sources and the
    "partial answer / what's missing" contract from graceful degradation: when
    a dependency fails, a tool returns what it could with `partial=True` and
    the un-retrieved pieces named in `missing`, rather than crashing or
    silently dropping data.
    """

    model_config = ConfigDict(extra="forbid")

    data: T
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="Source pointers grounding this result.",
    )
    retrieved_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the tool produced this result.",
    )
    partial: bool = Field(
        default=False,
        description="True when some requested data could not be retrieved.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="Names of the pieces that could not be retrieved.",
    )
