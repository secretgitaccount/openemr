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
from copilot.verification.knowledge import (
    canonical_classes,
    find_interaction,
    DOSAGE_CEILINGS,
    parse_daily_mg,
)

__all__ = [
    "RuleFlag",
    "check_allergy_contraindications",
    "check_drug_interactions",
    "check_dosage_thresholds",
    "run_all_rules",
]


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


# ---------------------------------------------------------------------------
# Drug-drug interaction rule
# ---------------------------------------------------------------------------


def _active_meds(critical_set: CriticalSet) -> list[Medication]:
    """Active medications only; a stopped med is not a live hazard."""

    return [m for m in critical_set.medications if m.status.strip().lower() == "active"]


def check_drug_interactions(critical_set: CriticalSet) -> list[RuleFlag]:
    """Flag interacting pairs among the active medications (FR-9, UC-4).

    Cross-products every pair of active meds against the demo-scale interaction
    table in :mod:`copilot.verification.knowledge`. Each match yields one
    :class:`RuleFlag` grounded in **both** medications' source records, so the
    danger surfaces from the retrieved data whether or not the model mentioned
    it. Deterministic: meds are compared in list order and each unordered pair
    is considered once.
    """

    meds = _active_meds(critical_set)
    classes = [canonical_classes(m.name) for m in meds]

    flags: list[RuleFlag] = []
    for i in range(len(meds)):
        for j in range(i + 1, len(meds)):
            interaction = find_interaction(classes[i], classes[j])
            if interaction is None:
                continue
            flags.append(
                RuleFlag(
                    rule="drug_interaction",
                    severity=interaction.severity,
                    message=(
                        f"Potential interaction between '{meds[i].name}' and "
                        f"'{meds[j].name}': {interaction.description}"
                    ),
                    sources=[meds[i].source, meds[j].source],
                )
            )
    return flags


# ---------------------------------------------------------------------------
# Dosage-threshold rule
# ---------------------------------------------------------------------------


def check_dosage_thresholds(critical_set: CriticalSet) -> list[RuleFlag]:
    """Flag active medications dosed above their maximum daily ceiling (FR-9).

    Parses each active med's ``dosage`` into a total milligrams-per-day and
    compares it to the per-drug ceiling in
    :mod:`copilot.verification.knowledge`. Meds whose dosage cannot be parsed
    (missing, no milligram amount, unrecognized) are **skipped** rather than
    flagged, so an unparseable dose never becomes a false positive. Each flag is
    grounded in the medication's own source record.
    """

    flags: list[RuleFlag] = []
    for med in _active_meds(critical_set):
        name = med.name.lower()
        for ceiling in DOSAGE_CEILINGS:
            if not any(syn in name for syn in ceiling.synonyms):
                continue
            daily_mg = parse_daily_mg(med.dosage)
            if daily_mg is None or daily_mg <= ceiling.max_mg_per_day:
                continue
            flags.append(
                RuleFlag(
                    rule="dosage_threshold",
                    severity="high",
                    message=(
                        f"Medication '{med.name}' appears dosed at "
                        f"{daily_mg:g} mg/day, above the {ceiling.max_mg_per_day:g} "
                        f"mg/day maximum for {ceiling.drug}."
                    ),
                    sources=[med.source],
                )
            )
            break
    return flags


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

#: Rank for deterministic severity ordering (lower sorts first).
_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def run_all_rules(critical_set: CriticalSet) -> list[RuleFlag]:
    """Run every deterministic safety rule over the retrieved data (FR-9).

    Unions the allergy-contraindication, drug-interaction, and dosage-threshold
    findings and returns them ordered by severity (``high`` first). The sort is
    stable, so within a severity the flags keep their rule-by-rule order,
    keeping the result deterministic for the same input.
    """

    flags = [
        *check_allergy_contraindications(critical_set),
        *check_drug_interactions(critical_set),
        *check_dosage_thresholds(critical_set),
    ]
    return sorted(flags, key=lambda f: _SEVERITY_RANK.get(f.severity, len(_SEVERITY_RANK)))
