"""Pydantic contract package (NFR-3): the source of truth for tool I/O.

Re-exports the cross-cutting core types plus the M1 clinical and
source-bound output contracts so callers can import from one place.
"""

from __future__ import annotations

from copilot.schemas.clinical import (
    Allergy,
    CriticalSet,
    Deltas,
    Encounter,
    LabResult,
    Medication,
    PanelDecision,
    Problem,
    ScheduledPatient,
)
from copilot.schemas.core import AgentError, SourceRef, TokenResponse, ToolResult
from copilot.schemas.output import Claim, GroundedSummary
from copilot.schemas.patient import Patient, Sex

__all__ = [
    # core
    "SourceRef",
    "TokenResponse",
    "AgentError",
    "ToolResult",
    # patient
    "Patient",
    "Sex",
    # clinical
    "ScheduledPatient",
    "PanelDecision",
    "Medication",
    "Allergy",
    "LabResult",
    "Problem",
    "Encounter",
    "Deltas",
    "CriticalSet",
    # output
    "Claim",
    "GroundedSummary",
]
