"""Tiered parallel critical-set retrieval over OpenEMR FHIR (PRP M1-3, FR-3/4/11).

This module owns the "must-know" retrieval tier: active medications, allergies,
recent/abnormal labs and the open problem list — each fetched **as the user**
(``user/*`` scope) through the shared :class:`~copilot.openemr.client.FhirClient`,
each record mapped into a typed, source-bound M1-1 clinical model so every claim
the agent surfaces can be grounded (FR-8).

Two contracts shape the code:

* **Defensive mapping.** FHIR search Bundles are loosely typed and
  optional-field-heavy; every access is guarded (mirroring
  :mod:`copilot.openemr.tools`) and a resource that cannot satisfy its contract
  is skipped rather than producing a half-valid model or crashing the whole
  fetch.
* **Graceful degradation (FR-11).** A per-tool failure never takes the bundle
  down. Each tool catches a failed fetch and returns a *partial*
  :class:`~copilot.schemas.core.ToolResult` naming what could not be retrieved;
  :func:`get_critical_set` fans the four out with
  ``asyncio.gather(return_exceptions=True)`` and folds any failure into
  :attr:`CriticalSet.missing`. Crucially this keeps **"no data on file"**
  (an empty list, ``partial=False``) distinct from **"not retrieved"**
  (``partial=True`` with the tier named in ``missing``) — e.g. an empty allergy
  result means *no known allergies*, not *allergies unavailable*.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from copilot.config import Settings, get_settings
from copilot.logging import configure_logging, new_correlation_id, set_correlation_id
from copilot.observability import flush, trace
from copilot.openemr.client import FhirClient, FhirError
from copilot.openemr.oauth import OAuthError, TokenProvider, register_client
from copilot.schemas.clinical import (
    Allergy,
    CriticalSet,
    LabResult,
    Medication,
    Problem,
)
from copilot.schemas.core import SourceRef, ToolResult

__all__ = [
    "get_active_medications",
    "get_allergies",
    "get_recent_labs",
    "get_problem_list",
    "get_critical_set",
]

# Tier labels used both as the ``missing`` names and the trace count keys.
_MEDICATIONS = "medications"
_ALLERGIES = "allergies"
_LABS = "labs"
_PROBLEMS = "problems"

# HL7 v3 ObservationInterpretation codes that flag a result as out-of-range.
_ABNORMAL_INTERP: frozenset[str] = frozenset(
    {"H", "HH", "HU", "L", "LL", "LU", "A", "AA", "AU", "H>", "L<"}
)
# Codes that explicitly assert a *normal* result.
_NORMAL_INTERP: frozenset[str] = frozenset({"N", "NR"})


# ---------------------------------------------------------------------------
# Generic FHIR helpers (guarded, PHI-agnostic)
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse a FHIR ``instant``/``dateTime`` string into a ``datetime``.

    Tolerates a trailing ``Z`` and returns ``None`` for anything unparseable —
    a missing timestamp weakens grounding but must not fail the read.
    """

    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    """Parse a FHIR ``date``/``dateTime`` into a ``date`` (``None`` if it can't)."""

    if not isinstance(value, str) or not value:
        return None
    # Accept a full timestamp too — take the date portion.
    ts = _parse_ts(value)
    if ts is not None:
        return ts.date()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _bundle_resources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the ``resource`` dicts from a FHIR search Bundle, guarded.

    Non-dict entries and entries without a resource object are skipped rather
    than raising — a malformed entry must not sink the whole search.
    """

    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return []
    resources: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if isinstance(resource, dict):
            resources.append(resource)
    return resources


def _codings(concept: Any) -> Iterable[dict[str, Any]]:
    """Yield the ``coding`` dicts of a FHIR CodeableConcept, guarded."""

    if not isinstance(concept, dict):
        return
    codings = concept.get("coding")
    if isinstance(codings, list):
        for coding in codings:
            if isinstance(coding, dict):
                yield coding


def _concept_text(concept: Any) -> str | None:
    """Best display text for a CodeableConcept: ``text`` → coding display → code."""

    if not isinstance(concept, dict):
        return None
    text = concept.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    for coding in _codings(concept):
        display = coding.get("display")
        if isinstance(display, str) and display.strip():
            return display.strip()
    for coding in _codings(concept):
        code = coding.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def _resource_id(resource: dict[str, Any]) -> str | None:
    """Return the resource id when it is a usable non-empty string."""

    rid = resource.get("id")
    return rid if isinstance(rid, str) and rid else None


def _source_ref(
    resource: dict[str, Any],
    resource_type: str,
    resource_id: str,
    *,
    timestamp: datetime | None = None,
) -> SourceRef:
    """Build a grounding :class:`SourceRef`, falling back to ``meta.lastUpdated``."""

    if timestamp is None:
        meta = resource.get("meta")
        last_updated = meta.get("lastUpdated") if isinstance(meta, dict) else None
        timestamp = _parse_ts(last_updated)
    return SourceRef(resource_type=resource_type, id=resource_id, timestamp=timestamp)


# ---------------------------------------------------------------------------
# FHIR -> schema mapping (one mapper per record type; each returns None on skip)
# ---------------------------------------------------------------------------


def _map_medication(resource: dict[str, Any]) -> Medication | None:
    """Map a FHIR ``MedicationRequest`` to :class:`Medication` (skip if unusable)."""

    rid = _resource_id(resource)
    if rid is None:
        return None
    name = _concept_text(resource.get("medicationCodeableConcept"))
    if name is None:
        # Some servers use a contained/referenced Medication; fall back to the
        # reference display if present.
        ref = resource.get("medicationReference")
        if isinstance(ref, dict) and isinstance(ref.get("display"), str):
            name = ref["display"].strip() or None
    if name is None:
        return None

    status = resource.get("status")
    status_str = status.strip() if isinstance(status, str) and status.strip() else "unknown"

    dosage = None
    instructions = resource.get("dosageInstruction")
    if isinstance(instructions, list):
        for instr in instructions:
            if isinstance(instr, dict) and isinstance(instr.get("text"), str):
                if instr["text"].strip():
                    dosage = instr["text"].strip()
                    break

    return Medication(
        id=rid,
        name=name,
        status=status_str,
        dosage=dosage,
        source=_source_ref(
            resource, "MedicationRequest", rid,
            timestamp=_parse_ts(resource.get("authoredOn")),
        ),
    )


def _map_allergy(resource: dict[str, Any]) -> Allergy | None:
    """Map a FHIR ``AllergyIntolerance`` to :class:`Allergy` (skip if unusable)."""

    rid = _resource_id(resource)
    if rid is None:
        return None
    substance = _concept_text(resource.get("code"))
    if substance is None:
        return None

    reaction = None
    reactions = resource.get("reaction")
    if isinstance(reactions, list):
        for entry in reactions:
            if not isinstance(entry, dict):
                continue
            manifestations = entry.get("manifestation")
            if isinstance(manifestations, list):
                for manifestation in manifestations:
                    reaction = _concept_text(manifestation)
                    if reaction is not None:
                        break
            if reaction is not None:
                break

    criticality = resource.get("criticality")
    criticality_str = (
        criticality.strip()
        if isinstance(criticality, str) and criticality.strip()
        else None
    )

    return Allergy(
        id=rid,
        substance=substance,
        reaction=reaction,
        criticality=criticality_str,
        source=_source_ref(
            resource, "AllergyIntolerance", rid,
            timestamp=_parse_ts(resource.get("recordedDate")),
        ),
    )


def _lab_value_unit(resource: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract a text value + unit from a lab ``Observation``'s ``value[x]``."""

    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict):
        value = quantity.get("value")
        unit = quantity.get("unit")
        value_str = str(value) if value is not None else None
        unit_str = unit.strip() if isinstance(unit, str) and unit.strip() else None
        return value_str, unit_str

    value_string = resource.get("valueString")
    if isinstance(value_string, str) and value_string.strip():
        return value_string.strip(), None

    concept_text = _concept_text(resource.get("valueCodeableConcept"))
    if concept_text is not None:
        return concept_text, None

    return None, None


def _lab_abnormal(resource: dict[str, Any]) -> bool | None:
    """Flag whether a lab result is abnormal, from interpretation then range.

    Returns ``True``/``False`` when it can be determined and ``None`` otherwise,
    so "unknown" stays distinct from "known-normal".
    """

    interpretations = resource.get("interpretation")
    if isinstance(interpretations, list):
        saw_normal = False
        for concept in interpretations:
            for coding in _codings(concept):
                code = coding.get("code")
                if not isinstance(code, str):
                    continue
                up = code.strip().upper()
                if up in _ABNORMAL_INTERP:
                    return True
                if up in _NORMAL_INTERP:
                    saw_normal = True
        if saw_normal:
            return False

    quantity = resource.get("valueQuantity")
    ranges = resource.get("referenceRange")
    if isinstance(quantity, dict) and isinstance(ranges, list):
        value = quantity.get("value")
        if isinstance(value, (int, float)):
            for rng in ranges:
                if not isinstance(rng, dict):
                    continue
                low = rng.get("low")
                high = rng.get("high")
                low_v = low.get("value") if isinstance(low, dict) else None
                high_v = high.get("value") if isinstance(high, dict) else None
                if isinstance(low_v, (int, float)) and value < low_v:
                    return True
                if isinstance(high_v, (int, float)) and value > high_v:
                    return True
                if isinstance(low_v, (int, float)) or isinstance(high_v, (int, float)):
                    return False

    return None


def _map_lab(resource: dict[str, Any]) -> LabResult | None:
    """Map a FHIR laboratory ``Observation`` to :class:`LabResult` (skip if unusable)."""

    rid = _resource_id(resource)
    if rid is None:
        return None
    name = _concept_text(resource.get("code"))
    if name is None:
        return None

    value, unit = _lab_value_unit(resource)
    effective = _parse_ts(resource.get("effectiveDateTime"))
    if effective is None:
        period = resource.get("effectivePeriod")
        if isinstance(period, dict):
            effective = _parse_ts(period.get("start"))

    return LabResult(
        id=rid,
        name=name,
        value=value,
        unit=unit,
        effective=effective,
        abnormal=_lab_abnormal(resource),
        source=_source_ref(resource, "Observation", rid, timestamp=effective),
    )


#: Most recent readings kept per distinct lab test for the summary. Trends need a
#: few points; every abnormal is kept regardless of age (see :func:`_bound_labs`).
_LABS_PER_TEST = 3


def _lab_recency(lab: LabResult) -> float:
    """Sort key — newer first; an undated result sinks to the bottom."""

    return lab.effective.timestamp() if lab.effective else float("-inf")


def _bound_labs(labs: list[LabResult], per_test: int = _LABS_PER_TEST) -> list[LabResult]:
    """Reduce a long lab history to the clinically-salient slice (minimum-necessary, NFR-4).

    FHIR models every analyte as its own ``Observation``, so a decade of
    comprehensive panels is thousands of result-level records — most of them
    redundant routine normals. This keeps the most recent ``per_test`` readings of
    each distinct test (trends survive) **plus every result flagged abnormal at any
    age** (nothing clinically significant is dropped), collapsing thousands of
    readings to a few hundred. The full record is untouched in OpenEMR and stays
    retrievable on demand; this only bounds what the summary reasons over.
    """

    by_test: dict[str, list[LabResult]] = {}
    for lab in labs:
        by_test.setdefault(lab.name, []).append(lab)

    kept: dict[str, LabResult] = {}  # id -> lab; de-dups across the two passes
    for group in by_test.values():
        for lab in sorted(group, key=_lab_recency, reverse=True)[:per_test]:
            kept[lab.id] = lab
    for lab in labs:  # never drop an abnormal, however old
        if lab.abnormal and lab.id not in kept:
            kept[lab.id] = lab

    return sorted(kept.values(), key=_lab_recency, reverse=True)


def _map_problem(resource: dict[str, Any]) -> Problem | None:
    """Map a FHIR ``Condition`` to :class:`Problem` (skip if unusable)."""

    rid = _resource_id(resource)
    if rid is None:
        return None
    name = _concept_text(resource.get("code"))
    if name is None:
        return None

    clinical_status = None
    status_concept = resource.get("clinicalStatus")
    for coding in _codings(status_concept):
        code = coding.get("code")
        if isinstance(code, str) and code.strip():
            clinical_status = code.strip()
            break
    if clinical_status is None:
        clinical_status = _concept_text(status_concept)

    onset = _parse_date(resource.get("onsetDateTime"))
    if onset is None:
        period = resource.get("onsetPeriod")
        if isinstance(period, dict):
            onset = _parse_date(period.get("start"))

    return Problem(
        id=rid,
        name=name,
        clinical_status=clinical_status,
        onset=onset,
        source=_source_ref(
            resource, "Condition", rid,
            timestamp=_parse_ts(resource.get("recordedDate")),
        ),
    )


# ---------------------------------------------------------------------------
# Tools — each returns a ToolResult and degrades to a partial on a failed fetch
# ---------------------------------------------------------------------------


def _map_all(
    resources: list[dict[str, Any]],
    mapper: Any,
) -> tuple[list[Any], list[SourceRef]]:
    """Map every resource with ``mapper``, dropping any that cannot be mapped."""

    records: list[Any] = []
    sources: list[SourceRef] = []
    for resource in resources:
        record = mapper(resource)
        if record is not None:
            records.append(record)
            sources.append(record.source)
    return records, sources


def _partial(name: str) -> ToolResult[list[Any]]:
    """A failed-fetch :class:`ToolResult`: empty data, ``partial``, tier named."""

    return ToolResult(data=[], sources=[], partial=True, missing=[name])


async def get_active_medications(
    patient_id: str,
    *,
    client: FhirClient,
) -> ToolResult[list[Medication]]:
    """Active medications for a patient (``MedicationRequest?status=active``).

    A failed fetch degrades to a partial result naming ``medications`` in
    :attr:`ToolResult.missing`; an empty (but successful) search is a genuine
    "no active medications on file".
    """

    try:
        bundle = await client.get(
            "/MedicationRequest",
            params={"patient": patient_id, "status": "active"},
        )
    except FhirError:
        return _partial(_MEDICATIONS)
    meds, sources = _map_all(_bundle_resources(bundle), _map_medication)
    return ToolResult(data=meds, sources=sources)


async def get_allergies(
    patient_id: str,
    *,
    client: FhirClient,
) -> ToolResult[list[Allergy]]:
    """Allergies / intolerances for a patient (``AllergyIntolerance?patient=``).

    FR-11 crux: an **empty** successful result is ``partial=False`` /
    ``missing=[]`` with ``data=[]`` — meaning **"no known allergies"**. A
    **failed** fetch is ``partial=True`` / ``missing=["allergies"]`` — meaning
    **"not retrieved"**. The two are never conflated.
    """

    try:
        bundle = await client.get(
            "/AllergyIntolerance",
            params={"patient": patient_id},
        )
    except FhirError:
        return _partial(_ALLERGIES)
    allergies, sources = _map_all(_bundle_resources(bundle), _map_allergy)
    return ToolResult(data=allergies, sources=sources)


async def get_recent_labs(
    patient_id: str,
    since: date | datetime | str | None = None,
    *,
    client: FhirClient,
    bound: bool = True,
) -> ToolResult[list[LabResult]]:
    """Recent laboratory results (``Observation?category=laboratory``).

    When ``since`` is given the search is bounded with ``date=ge<since>``. Each
    result carries an ``abnormal`` flag derived from FHIR ``interpretation`` or,
    failing that, the reference range. A failed fetch degrades to a partial
    result naming ``labs``.
    """

    params: dict[str, Any] = {"patient": patient_id, "category": "laboratory"}
    since_str = _since_param(since)
    if since_str is not None:
        params["date"] = f"ge{since_str}"

    try:
        bundle = await client.get("/Observation", params=params)
    except FhirError:
        return _partial(_LABS)
    all_labs, _ = _map_all(_bundle_resources(bundle), _map_lab)
    data = _bound_labs(all_labs) if bound else all_labs  # NFR-4 minimum-necessary
    return ToolResult(
        data=data,
        sources=[lab.source for lab in data],
        total_available=len(all_labs),  # free: whole bundle was fetched to bound it
    )


async def get_problem_list(
    patient_id: str,
    *,
    client: FhirClient,
) -> ToolResult[list[Problem]]:
    """Open problems for a patient (the problem list).

    Queries ``Condition?patient=`` and filters to active problems **client-side**:
    OpenEMR's FHIR does not honour the ``clinical-status`` search param (it
    returns an empty bundle), so filtering server-side silently drops the whole
    problem list. A failed fetch degrades to a partial result naming ``problems``.
    """

    try:
        bundle = await client.get("/Condition", params={"patient": patient_id})
    except FhirError:
        return _partial(_PROBLEMS)
    problems, _ = _map_all(_bundle_resources(bundle), _map_problem)
    # Keep active / unknown-status problems; drop only the explicitly closed ones.
    inactive = {"resolved", "inactive", "remission"}
    active = [p for p in problems if (p.clinical_status or "active").lower() not in inactive]
    return ToolResult(data=active, sources=[p.source for p in active])


def _since_param(since: date | datetime | str | None) -> str | None:
    """Normalise a ``since`` argument into a FHIR-searchable date string."""

    if since is None:
        return None
    if isinstance(since, datetime):
        return since.date().isoformat()
    if isinstance(since, date):
        return since.isoformat()
    text = since.strip()
    return text or None


# ---------------------------------------------------------------------------
# Parallel critical-set assembly (FR-4, FR-11)
# ---------------------------------------------------------------------------


def _fold(result: Any, name: str, missing: list[str]) -> list[Any]:
    """Fold one gather outcome into the bundle, recording any failure in ``missing``.

    Handles both degradation paths: a tool that caught its own failure (a
    partial :class:`ToolResult` carrying ``missing``) and a tool that raised
    outright (an exception captured by ``return_exceptions=True``).
    """

    if isinstance(result, BaseException):
        missing.append(name)
        return []
    if result.missing:
        missing.extend(result.missing)
    return list(result.data)


async def get_critical_set(
    patient_id: str,
    *,
    client: FhirClient,
    full_labs: bool = False,
) -> CriticalSet:
    """Assemble the tiered "must-know" bundle, fetching the four tiers in parallel.

    Fans the four critical-set tools out with
    ``asyncio.gather(return_exceptions=True)`` so a single-tier failure never
    raises: it is folded into :attr:`CriticalSet.missing` instead. The wrapping
    span records only counts and the ``missing`` tier names — never clinical
    values (NFR-4).
    """

    with trace(
        "get_critical_set",
        metadata={"resource_type": "CriticalSet", "patient_id": patient_id},
    ) as span:
        meds_r, allergy_r, labs_r, problems_r = await asyncio.gather(
            get_active_medications(patient_id, client=client),
            get_allergies(patient_id, client=client),
            get_recent_labs(patient_id, client=client, bound=not full_labs),
            get_problem_list(patient_id, client=client),
            return_exceptions=True,
        )

        missing: list[str] = []
        medications = _fold(meds_r, _MEDICATIONS, missing)
        allergies = _fold(allergy_r, _ALLERGIES, missing)
        labs = _fold(labs_r, _LABS, missing)
        problems = _fold(problems_r, _PROBLEMS, missing)

        # Labs available upstream but not analysed (bounded away) — free, since
        # get_recent_labs already fetched the whole bundle to bound it.
        labs_total = labs_r.total_available if isinstance(labs_r, ToolResult) else None
        labs_omitted = max(0, (labs_total if labs_total is not None else len(labs)) - len(labs))

        critical_set = CriticalSet(
            medications=medications,
            allergies=allergies,
            labs=labs,
            problems=problems,
            missing=missing,
            labs_omitted=labs_omitted,
        )
        span.update(
            metadata={
                "medications_count": len(medications),
                "allergies_count": len(allergies),
                "labs_count": len(labs),
                "labs_omitted": labs_omitted,
                "problems_count": len(problems),
                "missing": missing,
            }
        )
        return critical_set


# ---------------------------------------------------------------------------
# CLI smoke — live end-to-end check against local OpenEMR (M1 acceptance)
# ---------------------------------------------------------------------------


async def _smoke(patient_id: str) -> int:
    """Fetch a real patient's critical set as the user token; print counts."""

    configure_logging()
    settings: Settings = get_settings()
    correlation_id = new_correlation_id()
    set_correlation_id(correlation_id)
    print(f"Critical-set smoke — {settings.openemr_fhir_base}")
    print(f"  correlation_id: {correlation_id}")

    try:
        creds = register_client(settings=settings)
    except OAuthError as exc:
        print(f"BLOCKED: client registration failed: {exc}", file=sys.stderr)
        return 2

    provider = TokenProvider(
        settings.openemr_dev_user,
        settings.openemr_dev_pass,
        settings=settings,
        credentials=creds,
    )

    async with FhirClient(provider, settings=settings) as client:
        try:
            critical_set = await get_critical_set(patient_id, client=client)
        except OAuthError as exc:
            print(f"BLOCKED: token acquisition failed: {exc}", file=sys.stderr)
            return 2

    print("  CriticalSet (counts):")
    print(f"    medications: {len(critical_set.medications)}")
    print(f"    allergies:   {len(critical_set.allergies)}")
    print(f"    labs:        {len(critical_set.labs)}")
    print(f"    problems:    {len(critical_set.problems)}")
    if critical_set.allergies == [] and _ALLERGIES not in critical_set.missing:
        print("    (allergies: none on file — 'no known allergies')")
    print(f"  missing (not retrieved): {critical_set.missing or 'none'}")
    print("  retrieved_at:", critical_set.retrieved_at.isoformat())
    flush()
    print("SMOKE OK — parallel, typed, source-bound critical set.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.openemr.retrieval",
        description="Live smoke: parallel critical-set retrieval as the user token.",
    )
    parser.add_argument(
        "--patient",
        dest="patient",
        required=True,
        help="FHIR Patient id whose critical set to assemble.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_smoke(args.patient))


if __name__ == "__main__":
    raise SystemExit(main())
