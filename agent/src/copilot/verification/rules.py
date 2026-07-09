"""Deterministic clinical safety rules (FR-9).

The verification gate runs these **non-LLM** checks over the retrieved
:class:`~copilot.schemas.clinical.CriticalSet` regardless of what the model
said. The first rule is an **allergy contraindication** cross-check: if an
active medication matches a recorded allergen, a :class:`RuleFlag` is raised so
the danger surfaces even when the model omitted it.

Matching is intentionally conservative for M1: a case-insensitive substring
compare in either direction plus a small hand-curated synonym set (e.g. the
penicillin class). It is deterministic and side-effect free, so the same data
always yields the same flags.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from copilot.schemas.clinical import Allergy, CriticalSet, Medication
from copilot.schemas.core import SourceRef

__all__ = ["RuleFlag", "check_allergy_contraindications"]


class RuleFlag(BaseModel):
    """A deterministic safety finding raised by a verification rule (FR-9).

    Carries the grounding sources of the records that triggered it so the UI can
    link the warning back to the offending medication and allergy records.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: str = Field(min_length=1, description="Stable, machine-readable rule id.")
    severity: str = Field(min_length=1, description="Severity, e.g. 'high'.")
    message: str = Field(min_length=1, description="Human-readable description of the finding.")
    sources: list[SourceRef] = Field(
        default_factory=list,
        description="Source records that triggered the flag (grounding).",
    )


# ---------------------------------------------------------------------------
# Allergy contraindication rule
# ---------------------------------------------------------------------------

#: Small synonym groups so cross-reactive names match without a drug database.
#: Each frozenset holds lowercase tokens considered equivalent for matching.
_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "penicillin",
            "penicillins",
            "amoxicillin",
            "ampicillin",
            "augmentin",
            "amoxicillin-clavulanate",
            "dicloxacillin",
            "piperacillin",
            "nafcillin",
        }
    ),
    frozenset(
        {
            "sulfa",
            "sulfonamide",
            "sulfamethoxazole",
            "cotrimoxazole",
            "trimethoprim-sulfamethoxazole",
            "bactrim",
            "septra",
        }
    ),
    frozenset(
        {
            "aspirin",
            "asa",
            "acetylsalicylic",
            "acetylsalicylic-acid",
        }
    ),
    frozenset(
        {
            "cephalexin",
            "cefazolin",
            "ceftriaxone",
            "cefuroxime",
            "cephalosporin",
        }
    ),
)

#: Ignore very short tokens so incidental substrings never match spuriously.
_MIN_TOKEN_LEN = 4


def _tokens(name: str) -> set[str]:
    """Split a drug/allergen name into lowercase alphanumeric tokens."""

    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= _MIN_TOKEN_LEN}


def _synonym_hit(med_tokens: set[str], allergen_tokens: set[str]) -> bool:
    """True when a med token and an allergen token share a synonym group."""

    for group in _SYNONYM_GROUPS:
        if med_tokens & group and allergen_tokens & group:
            return True
    return False


def _is_contraindicated(med: Medication, allergy: Allergy) -> bool:
    """Decide whether ``med`` is contraindicated by ``allergy``.

    Deterministic: a case-insensitive substring match in either direction, or a
    shared synonym group. Short/blank tokens are ignored to avoid false hits.
    """

    med_name = med.name.strip().lower()
    allergen = allergy.substance.strip().lower()
    if not med_name or not allergen:
        return False

    med_tokens = _tokens(med.name)
    allergen_tokens = _tokens(allergy.substance)

    # Direct containment either direction, but only for meaningful tokens.
    for token in allergen_tokens:
        if token in med_name:
            return True
    for token in med_tokens:
        if token in allergen:
            return True

    return _synonym_hit(med_tokens, allergen_tokens)


def check_allergy_contraindications(critical_set: CriticalSet) -> list[RuleFlag]:
    """Flag active medications that match a recorded allergen (FR-9).

    Runs independently of the model output over the retrieved
    :class:`CriticalSet`. Only **active** medications are considered (a stopped
    med is not a live contraindication). Each match yields one
    :class:`RuleFlag` grounded in the med and allergy source records.
    """

    flags: list[RuleFlag] = []
    for med in critical_set.medications:
        if med.status.strip().lower() != "active":
            continue
        for allergy in critical_set.allergies:
            if not _is_contraindicated(med, allergy):
                continue
            flags.append(
                RuleFlag(
                    rule="allergy_contraindication",
                    severity="high",
                    message=(
                        f"Active medication '{med.name}' may be contraindicated by "
                        f"recorded allergy to '{allergy.substance}'."
                    ),
                    sources=[med.source, allergy.source],
                )
            )
    return flags
