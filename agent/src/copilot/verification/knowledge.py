"""Demo-scale clinical safety knowledge base (FR-9, UC-4).

A tiny, **hand-curated and source-cited** table of well-known drug-drug
interactions and per-drug maximum daily doses, plus the normalization helpers
the deterministic rules in :mod:`copilot.verification.rules` use to match a
free-text medication name to a drug class.

.. warning::
   This is intentionally a **demo-scale** knowledge base: a handful of
   textbook interactions and dosage ceilings, enough to demonstrate the
   verification engine. A real deployment MUST replace it with a licensed,
   continuously-maintained interaction/dosing database (e.g. First Databank,
   Lexicomp, or the RxNorm + NLM Drug Interaction API). The pairs and ceilings
   below carry short human-readable citations to public references so the
   provenance of each rule is auditable, but they are **not** a substitute for
   a curated clinical dataset.

Everything here is pure data plus deterministic, side-effect-free helpers: the
same medication name always normalizes to the same class set, so the rules that
consume this module are reproducible.
"""

from __future__ import annotations

import re
from typing import NamedTuple

__all__ = [
    "DrugInteraction",
    "DosageCeiling",
    "DRUG_CLASS_SYNONYMS",
    "INTERACTIONS",
    "DOSAGE_CEILINGS",
    "canonical_classes",
    "find_interaction",
    "parse_daily_mg",
]


# ---------------------------------------------------------------------------
# Drug-class normalization
# ---------------------------------------------------------------------------

#: Map a stable drug-class key to the lowercase names/synonyms that belong to
#: it. Matching is case-insensitive; multi-token synonyms (e.g. "ace inhibitor")
#: are matched as substrings, single tokens as whole words so an incidental
#: substring (``asa`` inside ``asacol``) never matches spuriously.
DRUG_CLASS_SYNONYMS: dict[str, frozenset[str]] = {
    "warfarin": frozenset({"warfarin", "coumadin", "jantoven"}),
    "aspirin": frozenset({"aspirin", "asa", "acetylsalicylic"}),
    "nsaid": frozenset(
        {
            "nsaid",
            "ibuprofen",
            "naproxen",
            "diclofenac",
            "ketorolac",
            "meloxicam",
            "indomethacin",
            "celecoxib",
        }
    ),
    "ace_inhibitor": frozenset(
        {
            "ace inhibitor",
            "lisinopril",
            "enalapril",
            "ramipril",
            "captopril",
            "benazepril",
            "quinapril",
        }
    ),
    "potassium_sparing_diuretic": frozenset(
        {
            "spironolactone",
            "eplerenone",
            "amiloride",
            "triamterene",
        }
    ),
    "potassium_supplement": frozenset(
        {
            "potassium chloride",
            "potassium",
            "kcl",
            "klor-con",
        }
    ),
    "statin": frozenset(
        {
            "statin",
            "simvastatin",
            "atorvastatin",
            "lovastatin",
            "pravastatin",
            "rosuvastatin",
        }
    ),
    "macrolide": frozenset(
        {
            "macrolide",
            "clarithromycin",
            "erythromycin",
            "azithromycin",
        }
    ),
}

#: Single-token synonyms shorter than this are matched only as whole tokens,
#: never as substrings, to avoid incidental hits.
_SUBSTRING_MIN_LEN = 4


def canonical_classes(name: str) -> frozenset[str]:
    """Return the set of drug-class keys a free-text medication ``name`` maps to.

    Case-insensitive. A class matches when one of its synonyms appears as a
    whole token in ``name`` or (for multi-token / sufficiently long synonyms) as
    a substring. A drug may belong to more than one class; an unrecognized name
    yields an empty set.
    """

    lowered = name.lower()
    tokens = {t for t in re.split(r"[^a-z0-9]+", lowered) if t}

    hits: set[str] = set()
    for cls, synonyms in DRUG_CLASS_SYNONYMS.items():
        for syn in synonyms:
            if syn in tokens:
                hits.add(cls)
                break
            if len(syn) >= _SUBSTRING_MIN_LEN and syn in lowered:
                hits.add(cls)
                break
    return frozenset(hits)


# ---------------------------------------------------------------------------
# Drug-drug interactions
# ---------------------------------------------------------------------------


class DrugInteraction(NamedTuple):
    """A documented interaction between two drug classes.

    ``classes`` is a two-element frozenset of :data:`DRUG_CLASS_SYNONYMS` keys;
    the interaction fires when one active med maps to each class.
    """

    classes: frozenset[str]
    severity: str
    description: str
    reference: str


#: Textbook drug-drug interactions. Each carries a short public citation; a real
#: deployment would source these from a licensed interaction database.
INTERACTIONS: tuple[DrugInteraction, ...] = (
    DrugInteraction(
        classes=frozenset({"warfarin", "nsaid"}),
        severity="high",
        description=(
            "Concurrent warfarin and an NSAID markedly increase bleeding risk "
            "(additive antiplatelet effect plus GI mucosal injury)."
        ),
        reference="FDA warfarin (Coumadin) label, Drug Interactions section.",
    ),
    DrugInteraction(
        classes=frozenset({"warfarin", "aspirin"}),
        severity="high",
        description=(
            "Concurrent warfarin and aspirin increase bleeding risk via additive "
            "anticoagulant/antiplatelet effects."
        ),
        reference="FDA warfarin (Coumadin) label, Drug Interactions section.",
    ),
    DrugInteraction(
        classes=frozenset({"ace_inhibitor", "potassium_sparing_diuretic"}),
        severity="high",
        description=(
            "An ACE inhibitor with a potassium-sparing diuretic can cause "
            "hyperkalemia; monitor serum potassium."
        ),
        reference="FDA lisinopril label, Drug Interactions (potassium).",
    ),
    DrugInteraction(
        classes=frozenset({"ace_inhibitor", "potassium_supplement"}),
        severity="high",
        description=(
            "An ACE inhibitor with potassium supplementation can cause "
            "hyperkalemia; monitor serum potassium."
        ),
        reference="FDA lisinopril label, Drug Interactions (potassium).",
    ),
    DrugInteraction(
        classes=frozenset({"statin", "macrolide"}),
        severity="high",
        description=(
            "Macrolides (CYP3A4 inhibitors) raise statin levels, increasing the "
            "risk of myopathy and rhabdomyolysis."
        ),
        reference="FDA simvastatin (Zocor) label, Contraindications/Interactions.",
    ),
)


def find_interaction(
    classes_a: frozenset[str], classes_b: frozenset[str]
) -> DrugInteraction | None:
    """Return the interaction between two class sets, or ``None``.

    Deterministic: iterates :data:`INTERACTIONS` in declaration order and
    returns the first rule whose two classes are covered one by each argument.
    Self-pairs (a class interacting with itself) are ignored.
    """

    for rule in INTERACTIONS:
        pair = tuple(rule.classes)
        if len(pair) != 2:
            continue
        first, second = pair
        if (first in classes_a and second in classes_b) or (
            first in classes_b and second in classes_a
        ):
            return rule
    return None


# ---------------------------------------------------------------------------
# Dosage ceilings
# ---------------------------------------------------------------------------


class DosageCeiling(NamedTuple):
    """A per-drug maximum recommended daily dose.

    ``synonyms`` are matched against a free-text medication name the same way
    :func:`canonical_classes` matches; ``max_mg_per_day`` is the ceiling above
    which a :class:`~copilot.verification.rules.RuleFlag` is raised.
    """

    drug: str
    synonyms: frozenset[str]
    max_mg_per_day: float
    reference: str


#: Maximum recommended daily doses for a handful of common drugs. Demo-scale;
#: a real deployment would use a licensed dosing database keyed by indication,
#: weight, renal function, etc.
DOSAGE_CEILINGS: tuple[DosageCeiling, ...] = (
    DosageCeiling(
        drug="acetaminophen",
        synonyms=frozenset({"acetaminophen", "paracetamol", "tylenol"}),
        max_mg_per_day=4000.0,
        reference="FDA acetaminophen labeling: max 4000 mg/day for adults.",
    ),
    DosageCeiling(
        drug="ibuprofen",
        synonyms=frozenset({"ibuprofen", "motrin", "advil"}),
        max_mg_per_day=3200.0,
        reference="FDA ibuprofen (Rx) labeling: max 3200 mg/day.",
    ),
    DosageCeiling(
        drug="metformin",
        synonyms=frozenset({"metformin", "glucophage"}),
        max_mg_per_day=2550.0,
        reference="FDA metformin (Glucophage) labeling: max 2550 mg/day.",
    ),
    DosageCeiling(
        drug="gabapentin",
        synonyms=frozenset({"gabapentin", "neurontin"}),
        max_mg_per_day=3600.0,
        reference="FDA gabapentin (Neurontin) labeling: max 3600 mg/day.",
    ),
    DosageCeiling(
        drug="lisinopril",
        synonyms=frozenset({"lisinopril", "prinivil", "zestril"}),
        max_mg_per_day=80.0,
        reference="FDA lisinopril labeling: max 80 mg/day.",
    ),
)


# ---------------------------------------------------------------------------
# Dosage-string parsing
# ---------------------------------------------------------------------------

#: A milligram quantity in a dosage string, e.g. "500 mg" or "1000mg".
_MG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mg\b")

#: An "every N hours" frequency, e.g. "every 6 hours", "q6h", "q 8 h".
_EVERY_HOURS_RE = re.compile(r"(?:every|q)\s*(\d+)\s*(?:h\b|hours?\b|hrs?\b)")

#: Named frequency phrases → administrations per day.
_FREQUENCY_PHRASES: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"\b(?:four\s+times|qid|q\.i\.d\.?)\b"), 4.0),
    (re.compile(r"\b(?:three\s+times|thrice|tid|t\.i\.d\.?)\b"), 3.0),
    (re.compile(r"\b(?:twice|two\s+times|bid|b\.i\.d\.?)\b"), 2.0),
    (re.compile(r"\b(?:once|daily|nightly|qd|q\.d\.?|qhs|qam|qpm|od)\b"), 1.0),
)


def _doses_per_day(dosage: str) -> float | None:
    """Best-effort administrations-per-day from a dosage string, or ``None``.

    Recognizes an explicit "every N hours" schedule and a small set of named
    frequency phrases (once/twice/three-times/four-times and their Latin
    abbreviations). Returns ``None`` when no frequency is recognizable.
    """

    lowered = dosage.lower()

    every = _EVERY_HOURS_RE.search(lowered)
    if every is not None:
        hours = float(every.group(1))
        if hours > 0:
            return 24.0 / hours

    for pattern, per_day in _FREQUENCY_PHRASES:
        if pattern.search(lowered):
            return per_day

    return None


def parse_daily_mg(dosage: str | None) -> float | None:
    """Parse a total-milligrams-per-day from a free-text dosage string.

    Returns ``None`` (rather than guessing) when the string is missing, carries
    no milligram amount, or is otherwise unparseable — the caller then skips the
    med rather than raising a false positive. When a milligram amount is present
    but no explicit frequency is stated, a single daily administration is
    assumed (a lone amount is treated as the per-day total).
    """

    if dosage is None:
        return None

    mg_match = _MG_RE.search(dosage.lower())
    if mg_match is None:
        return None

    per_dose_mg = float(mg_match.group(1))
    per_day = _doses_per_day(dosage)
    if per_day is None:
        per_day = 1.0
    return per_dose_mg * per_day
