"""What changed since the last visit — a diff against a moving reference (PRP M1-4, FR-5, UC-1).

The static chart shows the *current* state; this module reasons about *change*.
It picks a reference point — the visit **before** the most recent one — and
windows the patient's medications, problems, labs and encounters to what is new
since then, so a clinician opening the chart sees "what moved" rather than
re-reading everything (UC-1).

Two contracts, mirrored from :mod:`copilot.openemr.retrieval`:

* **Reuse, don't fork.** The FHIR→schema mappers and guarded parsing helpers
  already live in :mod:`~copilot.openemr.retrieval`; this module imports them
  (it owns no mapping of its own beyond :class:`Encounter`) so the two stay in
  lockstep. Labs windowing reuses :func:`~copilot.openemr.retrieval.get_recent_labs`
  wholesale.
* **Graceful degradation (FR-11).** A thin history is *not* an error: a patient
  with fewer than two dated encounters yields an **empty but valid**
  :class:`Deltas` (``reference_visit=None``), never a raised exception. A failed
  history fetch yields the same empty :class:`Deltas` but flags it via
  ``ToolResult.partial=True`` / ``missing=["deltas"]`` so "nothing changed" stays
  distinct from "couldn't tell".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from typing import Any

from copilot.config import Settings, get_settings
from copilot.logging import configure_logging, new_correlation_id, set_correlation_id
from copilot.observability import flush, trace
from copilot.openemr.client import FhirClient, FhirError
from copilot.openemr.oauth import OAuthError, TokenProvider, register_client
from copilot.openemr.retrieval import (
    _bundle_resources,
    _concept_text,
    _map_medication,
    _map_problem,
    _parse_ts,
    _resource_id,
    _source_ref,
    get_recent_labs,
)
from copilot.schemas.clinical import (
    Deltas,
    Encounter,
    LabResult,
    Medication,
    Problem,
)
from copilot.schemas.core import SourceRef, ToolResult

__all__ = ["get_encounters_since", "get_deltas_since_last_visit"]

# The single delta unit name (mirrors the tier labels in retrieval) used both as
# the ``missing`` marker and the trace metadata key.
_DELTAS = "deltas"

# MedicationRequest.status values that mean the order is no longer running. A
# windowed med with one of these is a *stopped* med; anything else is a *new*
# (started) med.
_STOPPED_MED_STATUS: frozenset[str] = frozenset(
    {"stopped", "cancelled", "completed", "ended", "entered-in-error"}
)


# ---------------------------------------------------------------------------
# FHIR -> schema mapping (only Encounter; the rest is reused from retrieval)
# ---------------------------------------------------------------------------


def _map_encounter(resource: dict[str, Any]) -> Encounter | None:
    """Map a FHIR ``Encounter`` to :class:`Encounter` (skip if unusable).

    Defensive like the retrieval mappers: a resource without a usable id is
    dropped rather than producing a half-valid model. ``kind`` prefers the
    ``class`` coding (display → code), falling back to the first ``type``
    CodeableConcept; ``start`` and the grounding timestamp come from
    ``period.start``.
    """

    rid = _resource_id(resource)
    if rid is None:
        return None

    kind: str | None = None
    encounter_class = resource.get("class")
    if isinstance(encounter_class, dict):
        display = encounter_class.get("display")
        code = encounter_class.get("code")
        if isinstance(display, str) and display.strip():
            kind = display.strip()
        elif isinstance(code, str) and code.strip():
            kind = code.strip()
    if kind is None:
        types = resource.get("type")
        if isinstance(types, list):
            for concept in types:
                kind = _concept_text(concept)
                if kind is not None:
                    break

    start: datetime | None = None
    period = resource.get("period")
    if isinstance(period, dict):
        start = _parse_ts(period.get("start"))

    return Encounter(
        id=rid,
        kind=kind,
        start=start,
        source=_source_ref(resource, "Encounter", rid, timestamp=start),
    )


# ---------------------------------------------------------------------------
# Windowing helpers
# ---------------------------------------------------------------------------


def _as_datetime(value: date | datetime | None) -> datetime | None:
    """Coerce a ``date``/``datetime`` to an aware ``datetime`` for comparison.

    A bare ``date`` is anchored to midnight UTC so it is comparable with the
    timezone-aware timestamps parsed from FHIR. ``None`` passes through.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _at_or_after(value: date | datetime | None, reference: datetime) -> bool:
    """True when ``value`` is known and lands on/after the reference visit."""

    coerced = _as_datetime(value)
    return coerced is not None and coerced >= reference


def _problem_timestamp(problem: Problem) -> date | datetime | None:
    """Best "when did this problem appear" signal: recordedDate, else onset."""

    return problem.source.timestamp or problem.onset


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def get_encounters_since(
    patient_id: str,
    since: date | datetime | str | None,
    *,
    client: FhirClient,
) -> ToolResult[list[Encounter]]:
    """Encounters for a patient, newest first, optionally bounded by ``since``.

    Queries ``Encounter?patient=&_sort=-date``; when ``since`` is given the
    result is additionally filtered to encounters starting on/after it. The list
    is sorted by ``start`` descending (undated encounters sort last) so callers
    can read the reference visit off the front. A failed fetch degrades to a
    partial result naming ``deltas`` in :attr:`ToolResult.missing` rather than
    raising (FR-11).
    """

    try:
        bundle = await client.get(
            "/Encounter",
            params={"patient": patient_id, "_sort": "-date"},
        )
    except FhirError:
        return ToolResult(data=[], sources=[], partial=True, missing=[_DELTAS])

    encounters: list[Encounter] = []
    for resource in _bundle_resources(bundle):
        encounter = _map_encounter(resource)
        if encounter is not None:
            encounters.append(encounter)

    since_dt = _as_datetime(since if not isinstance(since, str) else _parse_ts(since))
    if since_dt is not None:
        encounters = [e for e in encounters if _at_or_after(e.start, since_dt)]

    # Newest first; undated encounters (start is None) sink to the end.
    encounters.sort(
        key=lambda e: (e.start is not None, e.start or datetime.min.replace(tzinfo=UTC)),
        reverse=True,
    )
    sources = [e.source for e in encounters]
    return ToolResult(data=encounters, sources=sources)


async def _fetch_windowed_meds(
    patient_id: str,
    reference: datetime,
    *,
    client: FhirClient,
) -> tuple[list[Medication], list[Medication]]:
    """Meds authored on/after ``reference``, split into (new, stopped).

    A single ``MedicationRequest?patient=`` fetch is windowed by ``authoredOn``
    (the med's source timestamp); each windowed med is classed as *stopped* when
    its status is terminal, else *new* (started). Raises :class:`FhirError` on a
    failed fetch — the caller folds that into ``missing``.
    """

    bundle = await client.get("/MedicationRequest", params={"patient": patient_id})
    new_meds: list[Medication] = []
    stopped_meds: list[Medication] = []
    for resource in _bundle_resources(bundle):
        med = _map_medication(resource)
        if med is None or not _at_or_after(med.source.timestamp, reference):
            continue
        if med.status.lower() in _STOPPED_MED_STATUS:
            stopped_meds.append(med)
        else:
            new_meds.append(med)
    return new_meds, stopped_meds


async def _fetch_windowed_problems(
    patient_id: str,
    reference: datetime,
    *,
    client: FhirClient,
) -> list[Problem]:
    """Problems recorded/onset on/after ``reference`` (``Condition?patient=``).

    Raises :class:`FhirError` on a failed fetch — folded into ``missing`` above.
    """

    bundle = await client.get("/Condition", params={"patient": patient_id})
    problems: list[Problem] = []
    for resource in _bundle_resources(bundle):
        problem = _map_problem(resource)
        if problem is None:
            continue
        if _at_or_after(_problem_timestamp(problem), reference):
            problems.append(problem)
    return problems


async def get_deltas_since_last_visit(
    patient_id: str,
    *,
    client: FhirClient,
) -> ToolResult[Deltas]:
    """Compute what changed since the patient's second-most-recent visit (FR-5).

    The reference point is the encounter **before** the most recent one — the
    natural "since last visit" anchor. With fewer than two dated encounters
    there is nothing to diff against, so an **empty** :class:`Deltas`
    (``reference_visit=None``) is returned — a valid answer, not an error.

    Around that reference the tool windows medications (new vs stopped),
    problems, labs and encounters, keeping each record's :class:`SourceRef` so
    every change stays grounded (FR-8). Any tier whose fetch fails is folded into
    :attr:`ToolResult.missing` with ``partial=True`` rather than sinking the
    whole diff (FR-11); a failed **encounter history** fetch degrades to an empty
    ``Deltas`` flagged the same way.
    """

    with trace(
        "get_deltas_since_last_visit",
        metadata={"resource_type": "Deltas", "patient_id": patient_id},
    ) as span:
        history = await get_encounters_since(patient_id, None, client=client)
        if history.partial:
            # History itself could not be fetched — degrade to an empty Deltas.
            span.update(metadata={"missing": [_DELTAS], "reference_visit": None})
            return ToolResult(
                data=Deltas(reference_visit=None),
                sources=[],
                partial=True,
                missing=[_DELTAS],
            )

        encounters = history.data
        dated = [e for e in encounters if e.start is not None]
        if len(dated) < 2:
            # Thin history: no second visit to diff against. Valid, empty answer.
            span.update(
                metadata={"reference_visit": None, "encounter_count": len(encounters)}
            )
            return ToolResult(data=Deltas(reference_visit=None), sources=[])

        # `dated` is already newest-first; the visit before last is index 1.
        reference: datetime = dated[1].start  # type: ignore[assignment]
        new_encounters = [e for e in dated if e.start is not None and e.start > reference]

        missing: list[str] = []

        try:
            new_meds, stopped_meds = await _fetch_windowed_meds(
                patient_id, reference, client=client
            )
        except FhirError:
            new_meds, stopped_meds = [], []
            missing.append("medications")

        try:
            new_problems = await _fetch_windowed_problems(
                patient_id, reference, client=client
            )
        except FhirError:
            new_problems = []
            missing.append("problems")

        labs_result = await get_recent_labs(patient_id, since=reference, client=client)
        new_labs: list[LabResult] = list(labs_result.data)
        if labs_result.partial:
            missing.extend(labs_result.missing or ["labs"])

        deltas = Deltas(
            reference_visit=reference,
            new_meds=new_meds,
            stopped_meds=stopped_meds,
            new_problems=new_problems,
            new_labs=new_labs,
            new_encounters=new_encounters,
        )

        sources: list[SourceRef] = [
            *(m.source for m in new_meds),
            *(m.source for m in stopped_meds),
            *(p.source for p in new_problems),
            *(lab.source for lab in new_labs),
            *(e.source for e in new_encounters),
        ]

        span.update(
            metadata={
                "reference_visit": reference,
                "new_meds_count": len(new_meds),
                "stopped_meds_count": len(stopped_meds),
                "new_problems_count": len(new_problems),
                "new_labs_count": len(new_labs),
                "new_encounters_count": len(new_encounters),
                "missing": missing,
            }
        )
        return ToolResult(
            data=deltas,
            sources=sources,
            partial=bool(missing),
            missing=missing,
        )


# ---------------------------------------------------------------------------
# CLI smoke — live end-to-end check against local OpenEMR (M1 acceptance)
# ---------------------------------------------------------------------------


async def _smoke(patient_id: str) -> int:
    """Compute a real patient's deltas as the user token; print the reference + counts."""

    configure_logging()
    settings: Settings = get_settings()
    correlation_id = new_correlation_id()
    set_correlation_id(correlation_id)
    print(f"Deltas smoke — {settings.openemr_fhir_base}")
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
            result = await get_deltas_since_last_visit(patient_id, client=client)
        except OAuthError as exc:
            print(f"BLOCKED: token acquisition failed: {exc}", file=sys.stderr)
            return 2

    deltas = result.data
    ref = deltas.reference_visit.isoformat() if deltas.reference_visit else "none (<2 visits)"
    print("  Deltas since last visit:")
    print(f"    reference_visit: {ref}")
    print(f"    new_meds:        {len(deltas.new_meds)}")
    print(f"    stopped_meds:    {len(deltas.stopped_meds)}")
    print(f"    new_problems:    {len(deltas.new_problems)}")
    print(f"    new_labs:        {len(deltas.new_labs)}")
    print(f"    new_encounters:  {len(deltas.new_encounters)}")
    print(f"  missing (not retrieved): {result.missing or 'none'}")
    print(f"  source_count (grounding): {len(result.sources)}")
    flush()
    print("SMOKE OK — reference point + windowed, source-bound changes.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.openemr.deltas",
        description="Live smoke: compute deltas since the patient's last visit.",
    )
    parser.add_argument(
        "--patient",
        dest="patient",
        required=True,
        help="FHIR Patient id whose deltas to compute.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_smoke(args.patient))


if __name__ == "__main__":
    raise SystemExit(main())
