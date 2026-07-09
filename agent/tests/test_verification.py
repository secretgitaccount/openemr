"""Tests for the deterministic verification gate (PRP M1-6, FR-8/9/10).

Covers both halves of the trust boundary:

* **Grounding** — a claim citing a record absent from the retrieved
  :class:`CriticalSet` is dropped; a fully-grounded claim survives.
* **Allergy rule** — a planted med-vs-allergy pair is flagged even when the
  summary omits it, and unrelated med/allergen data yields no false positive.

No Anthropic API or live token is used; the gate is pure Python.
"""

from __future__ import annotations

from copilot.schemas.clinical import Allergy, CriticalSet, Medication
from copilot.schemas.core import SourceRef
from copilot.schemas.output import Claim, GroundedSummary
from copilot.verification.gate import VerifiedSummary, verify
from copilot.verification.rules import RuleFlag, check_allergy_contraindications


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _med(mid: str, name: str, status: str = "active") -> Medication:
    return Medication(
        id=mid,
        name=name,
        status=status,
        source=SourceRef(resource_type="MedicationRequest", id=mid),
    )


def _allergy(aid: str, substance: str) -> Allergy:
    return Allergy(
        id=aid,
        substance=substance,
        source=SourceRef(resource_type="AllergyIntolerance", id=aid),
    )


def _claim(text: str, *sources: SourceRef) -> Claim:
    return Claim(text=text, sources=list(sources))


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def test_ungrounded_claim_is_dropped() -> None:
    """A claim citing a record not in the critical set is dropped, not shown."""

    med = _med("m1", "Lisinopril")
    critical_set = CriticalSet(medications=[med])

    good = _claim("On lisinopril", med.source)
    bad = _claim("On a phantom drug", SourceRef(resource_type="MedicationRequest", id="ghost"))
    summary = GroundedSummary(headline="H", must_knows=[good, bad])

    result = verify(summary, critical_set)

    assert isinstance(result, VerifiedSummary)
    assert good in result.summary.must_knows
    assert bad not in result.summary.must_knows
    assert result.dropped == [bad]


def test_fully_grounded_claim_survives() -> None:
    """Every source valid → the claim is retained verbatim."""

    med = _med("m1", "Lisinopril")
    allergy = _allergy("a1", "Peanut")
    critical_set = CriticalSet(medications=[med], allergies=[allergy])

    claim = _claim("On lisinopril; peanut allergy", med.source, allergy.source)
    summary = GroundedSummary(headline="H", whats_changed=[claim])

    result = verify(summary, critical_set)

    assert result.dropped == []
    assert result.summary.whats_changed == [claim]


def test_claim_with_no_sources_is_dropped() -> None:
    """An ungrounded (sourceless) claim never passes the gate."""

    critical_set = CriticalSet()
    summary = GroundedSummary(headline="H", must_knows=[_claim("unsupported fact")])

    result = verify(summary, critical_set)

    assert result.summary.must_knows == []
    assert len(result.dropped) == 1


def test_partially_grounded_claim_is_dropped() -> None:
    """One bad source among valid ones still drops the whole claim."""

    med = _med("m1", "Lisinopril")
    critical_set = CriticalSet(medications=[med])
    claim = _claim(
        "mixed", med.source, SourceRef(resource_type="Observation", id="nope")
    )
    summary = GroundedSummary(headline="H", must_knows=[claim])

    result = verify(summary, critical_set)

    assert result.dropped == [claim]


# ---------------------------------------------------------------------------
# Allergy contraindication rule
# ---------------------------------------------------------------------------


def test_allergy_contraindication_flagged_even_when_summary_omits_it() -> None:
    """A penicillin allergy + amoxicillin med is flagged with no supporting claim."""

    med = _med("m1", "Amoxicillin 500mg")
    allergy = _allergy("a1", "Penicillin")
    critical_set = CriticalSet(medications=[med], allergies=[allergy])

    # Summary says nothing about the interaction.
    summary = GroundedSummary(headline="Routine visit")

    result = verify(summary, critical_set)

    assert len(result.flags) == 1
    flag = result.flags[0]
    assert flag.rule == "allergy_contraindication"
    assert flag.severity == "high"
    assert {(s.resource_type, s.id) for s in flag.sources} == {
        ("MedicationRequest", "m1"),
        ("AllergyIntolerance", "a1"),
    }


def test_direct_substance_match_is_flagged() -> None:
    """Exact substance name appearing in the med name matches."""

    cs = CriticalSet(medications=[_med("m1", "Aspirin 81mg")], allergies=[_allergy("a1", "aspirin")])
    flags = check_allergy_contraindications(cs)
    assert len(flags) == 1


def test_unrelated_med_and_allergen_no_false_positive() -> None:
    """An unrelated med and allergen produce no flag."""

    cs = CriticalSet(
        medications=[_med("m1", "Lisinopril")],
        allergies=[_allergy("a1", "Peanut")],
    )
    assert check_allergy_contraindications(cs) == []


def test_inactive_medication_is_not_flagged() -> None:
    """A stopped med matching an allergen is not a live contraindication."""

    cs = CriticalSet(
        medications=[_med("m1", "Amoxicillin", status="stopped")],
        allergies=[_allergy("a1", "Penicillin")],
    )
    assert check_allergy_contraindications(cs) == []


def test_rule_flag_is_frozen_and_typed() -> None:
    """RuleFlag is a well-formed frozen contract."""

    flag = RuleFlag(rule="r", severity="high", message="m")
    assert flag.sources == []
