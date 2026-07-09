"""Patient demographic contracts.

Minimal for M0 — just enough to prove a grounded FHIR read end-to-end (M0-7).
Richer clinical models arrive with the retrieval tools in M1.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from copilot.schemas.core import SourceRef

__all__ = ["Patient", "Sex"]

# FHIR administrative gender value set.
Sex = Literal["male", "female", "other", "unknown"]


class Patient(BaseModel):
    """Minimal patient demographics carrying its own grounding source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="FHIR Patient resource id.")
    name: str = Field(min_length=1, description="Display name.")
    dob: date = Field(description="Date of birth.")
    sex: Sex = Field(description="Administrative gender.")
    source: SourceRef = Field(description="Pointer to the source record (grounding).")
