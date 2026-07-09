"""Tests for the full deterministic rule engine (PRP M2-1, FR-9, UC-4).

Covers the two rules M2-1 adds on top of the M1 allergy check —
**drug-drug interactions** and **dosage thresholds** — plus the aggregate
:func:`run_all_rules` and the demo-scale knowledge base helpers. Every rule
fires from the retrieved :class:`CriticalSet` alone, independent of any model
output; no Anthropic API or live token is involved (pure Python).
"""

from __future__ import annotations

from copilot.schemas.clinical import Allergy, CriticalSet, Medication
from copilot.schemas.core import SourceRef
from copilot.schemas.output import GroundedSummary
from copilot.verification.gate import verify
from copilot.verification.knowledge import (
    canonical_classes,
    find_interaction,
    parse_daily_mg,
)
from copilot.verification.rules import (
    RuleFlag,
    check_dosage_thresholds,
    check_drug_interactions,
    run_all_rules,
)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _med(
    mid: str, name: str, *, status: str = "active", dosage: str | None = None
) -> Medication:
    return Medication(
        id=mid,
        name=name,
        status=status,
        dosage=dosage,
        source=SourceRef(resource_type="MedicationRequest", id=mid),
    )


def _allergy(aid: str, substance: str) -> Allergy:
    return Allergy(
        id=aid,
        substance=substance,
        source=SourceRef(resource_type="AllergyIntolerance", id=aid),
    )


# ---------------------------------------------------------------------------
# Knowledge-base helpers
# ---------------------------------------------------------------------------


def test_canonical_classes_normalizes_synonyms_case_insensitively() -> None:
    assert "warfarin" in canonical_classes("Coumadin 5mg")
    assert "warfarin" in canonical_classes("WARFARIN sodium")
    assert "nsaid" in canonical_classes("Ibuprofen 200 mg")
    assert canonical_classes("Lisinopril") == frozenset({"ace_inhibitor"})


def test_canonical_classes_unknown_drug_is_empty() -> None:
    assert canonical_classes("Multivitamin") == frozenset()


def test_short_synonym_not_matched_as_substring() -> None:
    # "asa" must not match inside "asacol" (mesalamine brand).
    assert "aspirin" not in canonical_classes("Asacol 400mg")


def test_find_interaction_is_order_independent() -> None:
    warfarin = canonical_classes("Warfarin")
    aspirin = canonical_classes("Aspirin 81mg")
    assert find_interaction(warfarin, aspirin) is not None
    assert find_interaction(aspirin, warfarin) is not None
    assert find_interaction(canonical_classes("Lisinopril"), aspirin) is None


def test_parse_daily_mg() -> None:
    assert parse_daily_mg(None) is None
    assert parse_daily_mg("one tablet po daily") is None  # no mg amount
    assert parse_daily_mg("500 mg twice daily") == 1000.0
    assert parse_daily_mg("1000 mg every 6 hours") == 4000.0
    assert parse_daily_mg("650mg q4h") == 3900.0
    assert parse_daily_mg("500 mg") == 500.0  # lone amount → once daily


# ---------------------------------------------------------------------------
# Drug-drug interactions
# ---------------------------------------------------------------------------


def test_planted_interaction_flagged_even_when_summary_omits_it() -> None:
    """warfarin + aspirin both active → an interaction flag from the data alone."""

    cs = CriticalSet(
        medications=[_med("m1", "Warfarin 5mg"), _med("m2", "Aspirin 81mg")],
    )

    flags = check_drug_interactions(cs)
    assert len(flags) == 1
    flag = flags[0]
    assert flag.rule == "drug_interaction"
    assert flag.severity == "high"
    assert {(s.resource_type, s.id) for s in flag.sources} == {
        ("MedicationRequest", "m1"),
        ("MedicationRequest", "m2"),
    }

    # The gate surfaces it with no supporting claim in the summary.
    verified = verify(GroundedSummary(headline="Routine visit"), cs)
    assert any(f.rule == "drug_interaction" for f in verified.flags)


def test_ace_inhibitor_plus_potassium_sparing_flagged() -> None:
    cs = CriticalSet(
        medications=[_med("m1", "Lisinopril 10mg"), _med("m2", "Spironolactone 25mg")],
    )
    flags = check_drug_interactions(cs)
    assert len(flags) == 1
    assert flags[0].severity == "high"


def test_unrelated_meds_yield_no_interaction() -> None:
    cs = CriticalSet(
        medications=[_med("m1", "Lisinopril"), _med("m2", "Metformin 500mg")],
    )
    assert check_drug_interactions(cs) == []


def test_interaction_ignores_inactive_med() -> None:
    cs = CriticalSet(
        medications=[
            _med("m1", "Warfarin"),
            _med("m2", "Aspirin", status="stopped"),
        ],
    )
    assert check_drug_interactions(cs) == []


# ---------------------------------------------------------------------------
# Dosage thresholds
# ---------------------------------------------------------------------------


def test_over_ceiling_dosage_is_flagged() -> None:
    cs = CriticalSet(
        medications=[_med("m1", "Acetaminophen 1000mg", dosage="1000 mg every 4 hours")],
    )
    flags = check_dosage_thresholds(cs)
    assert len(flags) == 1
    flag = flags[0]
    assert flag.rule == "dosage_threshold"
    assert flag.severity == "high"
    assert {(s.resource_type, s.id) for s in flag.sources} == {("MedicationRequest", "m1")}


def test_under_ceiling_dosage_not_flagged() -> None:
    cs = CriticalSet(
        medications=[_med("m1", "Acetaminophen 500mg", dosage="500 mg twice daily")],
    )
    assert check_dosage_thresholds(cs) == []


def test_at_ceiling_dosage_not_flagged() -> None:
    # Exactly 4000 mg/day is at the ceiling, not above it.
    cs = CriticalSet(
        medications=[_med("m1", "Acetaminophen", dosage="1000 mg every 6 hours")],
    )
    assert check_dosage_thresholds(cs) == []


def test_unparseable_dosage_not_flagged() -> None:
    cs = CriticalSet(
        medications=[
            _med("m1", "Acetaminophen", dosage=None),
            _med("m2", "Acetaminophen", dosage="take as directed"),
        ],
    )
    assert check_dosage_thresholds(cs) == []


def test_unknown_drug_dosage_not_flagged() -> None:
    cs = CriticalSet(
        medications=[_med("m1", "Multivitamin 9000mg", dosage="9000 mg every 4 hours")],
    )
    assert check_dosage_thresholds(cs) == []


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def test_run_all_rules_unions_and_orders_by_severity() -> None:
    cs = CriticalSet(
        medications=[
            _med("m1", "Amoxicillin 500mg"),  # allergy contraindication (high)
            _med("m2", "Warfarin"),
            _med("m3", "Aspirin 81mg"),  # interaction with warfarin (high)
            _med(
                "m4",
                "Acetaminophen",
                dosage="1000 mg every 4 hours",
            ),  # dosage (high)
        ],
        allergies=[_allergy("a1", "Penicillin")],
    )

    flags = run_all_rules(cs)
    rules = {f.rule for f in flags}
    assert rules == {"allergy_contraindication", "drug_interaction", "dosage_threshold"}

    # Ordered by severity rank (all high here) and deterministic.
    ranks = [{"high": 0, "medium": 1, "low": 2}.get(f.severity, 9) for f in flags]
    assert ranks == sorted(ranks)
    assert run_all_rules(cs) == flags  # reproducible


def test_run_all_rules_clean_patient_has_no_flags() -> None:
    cs = CriticalSet(
        medications=[_med("m1", "Lisinopril", dosage="10 mg daily")],
        allergies=[_allergy("a1", "Peanut")],
    )
    assert run_all_rules(cs) == []


def test_rule_flag_still_frozen_and_typed() -> None:
    flag = RuleFlag(rule="r", severity="high", message="m")
    assert flag.sources == []
