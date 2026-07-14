"""Typed graph state + public I/O contracts for the LangGraph run (PRP-09).

The supervisor/worker graph (FR-5) threads one :class:`GraphState` through its
channels. The design keeps three things inspectable and PHI-safe:

* **:class:`Handoff`** — an immutable routing record. Every supervisor decision
  appends one (``from_node`` → ``to_node`` with a human-readable ``reason`` and a
  timestamp), so the *whole* routing history can be replayed from state alone.
* **:class:`GraphState`** — a LangGraph ``TypedDict`` of typed channels. Only the
  ``handoffs`` channel uses an append reducer (``operator.add``); every other
  channel takes last-write-wins. The number of routing decisions is therefore
  simply ``len(handoffs)`` — no separate step counter channel is needed, which
  keeps the state exactly the fields the contract names.
* **:class:`GraphInput` / :class:`GraphResult`** — the frozen, ``extra="forbid"``
  public boundary of :func:`copilot.graph.supervisor.run_graph`.
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "Attachment",
    "Handoff",
    "GraphState",
    "GraphInput",
    "GraphResult",
]


class Attachment(BaseModel):
    """A document attached to / referenced by the question, awaiting extraction.

    A document is named **exactly one** of two ways and ``doc_type`` selects the
    extraction path (``lab_pdf`` / ``intake_form``):

    * ``file_path`` — a source document the doctor uploaded through our tool
      (the PRP-06 ``attach_and_extract`` upload path); or
    * ``document_id`` — the stable OpenEMR id of a document **already in the
      chart** (the PRP-15 chart-read path), so ``/ask`` can ground against the
      very document the doctor is reading and reuse that read's extraction
      (PRP-17) instead of re-uploading or re-running the VLM.

    Exactly one of the two must be set (enforced by :meth:`_exactly_one_source`).
    Frozen + ``extra="forbid"`` like every other contract in the codebase.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_type: str = Field(description="PRP-06 doc type, e.g. 'lab_pdf' / 'intake_form'.")
    file_path: str | None = Field(
        default=None,
        description="Path to an uploaded source document to extract (upload path).",
    )
    document_id: str | None = Field(
        default=None,
        description="Stable OpenEMR id of a chart document to extract (chart-read path).",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Attachment:
        """Require exactly one of ``file_path`` / ``document_id`` (never both/neither)."""

        if bool(self.file_path) == bool(self.document_id):
            raise ValueError(
                "Attachment requires exactly one of 'file_path' or 'document_id'."
            )
        return self


class Handoff(BaseModel):
    """One inspectable routing decision made by the supervisor.

    Records where control passed *from* and *to* (``to_node == "done"`` marks
    termination), the human-readable ``reason`` the supervisor chose that route,
    and ``at`` (when the decision was made). Frozen so a logged handoff can never
    be rewritten after the fact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_node: str = Field(description="Node that made the routing decision.")
    to_node: str = Field(description="Node control passed to ('done' = terminate).")
    reason: str = Field(description="Human-readable justification for the route.")
    at: datetime = Field(description="When the decision was made (UTC).")


class GraphState(TypedDict):
    """LangGraph channel state threaded through the supervisor/worker graph.

    ``handoffs`` accumulates (append reducer) so the full routing log survives
    across super-steps; all other channels are last-write-wins. Carries the
    ``correlation_id`` so every node/span can be tied back to the originating
    request (NFR-2).
    """

    correlation_id: str
    patient_id: str
    question: str
    attachments: list[Attachment]
    extracted: list[Any]
    evidence: list[Any]
    handoffs: Annotated[list[Handoff], operator.add]
    done: bool


class GraphInput(BaseModel):
    """The request handed to :func:`run_graph`.

    ``correlation_id`` is optional — when absent the runner reuses the active
    request's ID or mints a fresh one so every graph run is always traceable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    patient_id: str = Field(description="Patient the question is scoped to.")
    question: str = Field(description="The clinical question to answer.")
    attachments: list[Attachment] = Field(
        default_factory=list,
        description="Documents to extract before answering (may be empty).",
    )
    correlation_id: str | None = Field(
        default=None,
        description="Optional inbound correlation ID; minted when absent.",
    )


class GraphResult(BaseModel):
    """The inspectable result of a graph run.

    Exposes the extracted facts, the retrieved evidence, and — crucially — the
    full ``handoffs`` routing log, so a caller (or QA) can replay exactly how the
    supervisor routed. ``steps`` is the number of routing decisions
    (``== len(handoffs)``). Frozen + ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: str
    patient_id: str
    question: str
    extracted: list[Any] = Field(default_factory=list)
    evidence: list[Any] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    done: bool = False
    steps: int = Field(default=0, description="Number of routing decisions made.")
