"""Patient-panel gate — runs *before* any clinical retrieval (FR-1, FR-2).

This is the trust boundary that decides whether the agent is even allowed to
read a patient's chart. A clinician sees a patient because that patient is on
their **schedule** today (or, failing a schedule lookup, because they have a
recent **encounter** with them). Anyone else is out of panel: the gate refuses,
explains why, and logs the refusal. Access to an out-of-panel patient is only
possible via an explicit, logged **break-glass** override.

Design mirrors ``copilot.openemr.tools``:

* every entry point is wrapped in a :func:`~copilot.observability.trace` span
  (correlation-tagged, PHI-scrubbed);
* FHIR resources are loosely typed, so mapping is defensive — an appointment or
  encounter that cannot ground a decision is skipped rather than trusted;
* every in-panel decision carries a grounding :class:`SourceRef` to the
  Appointment / Encounter that backs it (FR-8), so the "why" is auditable.

The gate reuses the built :class:`~copilot.openemr.client.FhirClient` (so the
reads happen as the logged-in clinician, FR-3) and never edits
``openemr/tools.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from copilot.audit import audit_break_glass, audit_refusal
from copilot.config import Settings, get_settings
from copilot.logging import configure_logging, new_correlation_id, set_correlation_id
from copilot.observability import flush, trace
from copilot.openemr.client import FhirClient, FhirError
from copilot.openemr.oauth import OAuthError, TokenProvider, register_client
from copilot.schemas.clinical import PanelDecision, ScheduledPatient
from copilot.schemas.core import SourceRef, ToolResult

__all__ = ["get_todays_schedule", "is_patient_in_panel", "break_glass"]


# ---------------------------------------------------------------------------
# FHIR helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse a FHIR ``instant``/``dateTime`` string into a ``datetime``.

    Tolerates a trailing ``Z`` and returns ``None`` for anything unparseable
    rather than raising — a missing timestamp weakens grounding but must not
    fail the gate.
    """

    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _bundle_resources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the resource dicts from a FHIR search-set ``Bundle`` (defensive)."""

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


def _ref_id(reference: Any, resource_type: str) -> str | None:
    """Extract the id from a ``"<Type>/<id>"`` FHIR reference string."""

    prefix = f"{resource_type}/"
    if isinstance(reference, str) and reference.startswith(prefix):
        rid = reference[len(prefix) :]
        return rid or None
    return None


def _appointment_patient(resource: dict[str, Any]) -> tuple[str, str | None] | None:
    """Return ``(patient_id, display_name)`` for an Appointment's patient actor.

    Returns ``None`` when no patient participant can be identified.
    """

    participants = resource.get("participant")
    if not isinstance(participants, list):
        return None
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        actor = participant.get("actor")
        if not isinstance(actor, dict):
            continue
        patient_id = _ref_id(actor.get("reference"), "Patient")
        if patient_id is not None:
            display = actor.get("display")
            return (patient_id, display if isinstance(display, str) and display else None)
    return None


def _map_scheduled(resource: dict[str, Any]) -> ScheduledPatient | None:
    """Map a raw FHIR ``Appointment`` to a :class:`ScheduledPatient`, or ``None``.

    Returns ``None`` (skip) when the appointment lacks the fields the contract
    requires — an id, a patient participant, and a start time — rather than
    fabricating a half-valid schedule entry.
    """

    if resource.get("resourceType") != "Appointment":
        return None
    appointment_id = resource.get("id")
    if not isinstance(appointment_id, str) or not appointment_id:
        return None
    patient = _appointment_patient(resource)
    if patient is None:
        return None
    patient_id, display = patient
    start = _parse_ts(resource.get("start"))
    if start is None:
        return None
    return ScheduledPatient(
        patient_id=patient_id,
        name=display or f"Patient {patient_id}",
        start=start,
        appointment_id=appointment_id,
        source=SourceRef(resource_type="Appointment", id=appointment_id, timestamp=start),
    )


def _encounter_source(resource: dict[str, Any]) -> SourceRef | None:
    """Build a grounding :class:`SourceRef` from a FHIR ``Encounter`` resource."""

    if resource.get("resourceType") != "Encounter":
        return None
    encounter_id = resource.get("id")
    if not isinstance(encounter_id, str) or not encounter_id:
        return None
    period = resource.get("period")
    start = _parse_ts(period.get("start")) if isinstance(period, dict) else None
    return SourceRef(resource_type="Encounter", id=encounter_id, timestamp=start)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


async def get_todays_schedule(
    provider_id: str,
    *,
    client: FhirClient,
) -> ToolResult[list[ScheduledPatient]]:
    """Return the provider's patients scheduled for today (FR-1).

    ``GET {FHIR}/Appointment?practitioner=<provider>&date=<today>`` mapped to a
    list of grounded :class:`ScheduledPatient`. Degrades gracefully: if the
    appointment index is unavailable the result is empty with ``partial=True``
    and ``missing=["schedule"]`` rather than an exception, so callers (notably
    :func:`is_patient_in_panel`) can fall back to the encounter check.
    """

    with trace("get_todays_schedule", metadata={"provider_id": provider_id}) as span:
        today = datetime.now(UTC).date().isoformat()
        try:
            bundle = await client.get(
                "/Appointment",
                params={"practitioner": provider_id, "date": today},
            )
        except FhirError:
            span.update(metadata={"partial": True, "missing": ["schedule"]})
            return ToolResult(data=[], partial=True, missing=["schedule"])

        patients = [
            mapped
            for resource in _bundle_resources(bundle)
            if (mapped := _map_scheduled(resource)) is not None
        ]
        sources = [p.source for p in patients]
        result: ToolResult[list[ScheduledPatient]] = ToolResult(data=patients, sources=sources)
        span.update(
            output={"resource_type": "Appointment", "count": len(patients)},
            metadata={"source_count": len(sources)},
        )
        return result


async def _recent_encounter_source(
    patient_id: str,
    provider_id: str,
    *,
    client: FhirClient,
) -> SourceRef | None:
    """Ground a fallback panel decision on a recent encounter with the provider.

    ``GET {FHIR}/Encounter?patient=<patient>&practitioner=<provider>`` (most
    recent first). Returns the encounter's :class:`SourceRef` if any exists,
    else ``None``. Swallows :class:`FhirError` — an unavailable encounter index
    simply means no fallback grounding, not a gate failure.
    """

    try:
        bundle = await client.get(
            "/Encounter",
            params={
                "patient": patient_id,
                "practitioner": provider_id,
                "_count": 1,
                "_sort": "-date",
            },
        )
    except FhirError:
        return None
    for resource in _bundle_resources(bundle):
        source = _encounter_source(resource)
        if source is not None:
            return source
    return None


async def is_patient_in_panel(
    patient_id: str,
    provider_id: str,
    *,
    client: FhirClient,
) -> ToolResult[PanelDecision]:
    """Decide whether ``patient_id`` is in ``provider_id``'s panel (FR-2).

    In panel when the patient is on today's schedule, or (fallback) has a recent
    encounter with the provider; the decision is grounded with a
    :class:`SourceRef` to whichever record backs it. Otherwise the decision is
    out-of-panel with a human-readable reason, and the refusal is audited — no
    clinical retrieval happens for an out-of-panel patient.
    """

    with trace(
        "is_patient_in_panel",
        metadata={"patient_id": patient_id, "provider_id": provider_id},
    ) as span:
        schedule = await get_todays_schedule(provider_id, client=client)
        scheduled = next((p for p in schedule.data if p.patient_id == patient_id), None)
        if scheduled is not None:
            decision = PanelDecision(
                in_panel=True,
                reason="Patient is on the provider's schedule today.",
                source=scheduled.source,
            )
            span.update(output={"in_panel": True}, metadata={"basis": "schedule"})
            return ToolResult(data=decision, sources=[scheduled.source])

        encounter_source = await _recent_encounter_source(
            patient_id, provider_id, client=client
        )
        if encounter_source is not None:
            decision = PanelDecision(
                in_panel=True,
                reason="Patient has a recent encounter with the provider.",
                source=encounter_source,
            )
            span.update(output={"in_panel": True}, metadata={"basis": "encounter"})
            return ToolResult(data=decision, sources=[encounter_source])

        reason = (
            "Patient is not on the provider's schedule today and has no recent "
            "encounter with them; access refused without a break-glass override."
        )
        audit_refusal(patient_id, provider_id, reason)
        decision = PanelDecision(in_panel=False, reason=reason)
        span.update(output={"in_panel": False}, metadata={"basis": "refused"})
        return ToolResult(data=decision)


async def break_glass(
    patient_id: str,
    provider_id: str,
    reason: str,
    *,
    client: FhirClient,
) -> ToolResult[PanelDecision]:
    """Grant out-of-panel access via an explicit, logged break-glass override.

    Returns an in-panel decision flagged ``break_glass=True`` and emits an audit
    event carrying the justification. Refuses an empty/whitespace reason with a
    :class:`ValueError`: an override with no recorded reason is not auditable and
    is therefore not permitted. ``client`` is accepted for call-site symmetry
    with the other gate functions (the override itself makes no FHIR read).
    """

    with trace(
        "break_glass",
        metadata={"patient_id": patient_id, "provider_id": provider_id},
    ) as span:
        cleaned = reason.strip() if isinstance(reason, str) else ""
        if not cleaned:
            raise ValueError("break_glass requires a non-empty reason")

        audit_break_glass(patient_id, provider_id, cleaned)
        decision = PanelDecision(
            in_panel=True,
            reason=f"Break-glass override: {cleaned}",
            break_glass=True,
        )
        span.update(output={"in_panel": True, "break_glass": True})
        return ToolResult(data=decision)


# ---------------------------------------------------------------------------
# CLI smoke — live end-to-end check against local OpenEMR (M1 acceptance)
# ---------------------------------------------------------------------------


def _print_decision(patient_id: str, result: ToolResult[PanelDecision]) -> None:
    decision = result.data
    print(f"  patient:      {patient_id}")
    print(f"  in_panel:     {decision.in_panel}")
    print(f"  break_glass:  {decision.break_glass}")
    print(f"  reason:       {decision.reason}")
    if decision.source is not None:
        ts = decision.source.timestamp.isoformat() if decision.source.timestamp else "unknown"
        print(f"  source:       {decision.source.resource_type}/{decision.source.id} @ {ts}")
    else:
        print("  source:       (none — refusal logged)")


async def _smoke(provider_id: str, patient_id: str, reason: str | None) -> int:
    """Run the panel gate against a real patient; print the grounded decision."""

    configure_logging()
    settings: Settings = get_settings()
    correlation_id = new_correlation_id()
    set_correlation_id(correlation_id)
    print(f"Panel-gate smoke — {settings.openemr_fhir_base}")
    print(f"  correlation_id: {correlation_id}")
    print(f"  provider:       {provider_id}")

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
            if reason is not None:
                result = await break_glass(patient_id, provider_id, reason, client=client)
            else:
                result = await is_patient_in_panel(patient_id, provider_id, client=client)
        except OAuthError as exc:
            print(f"BLOCKED: token acquisition failed: {exc}", file=sys.stderr)
            return 2
        except FhirError as exc:
            print(f"FAILED: FHIR read failed: {exc}", file=sys.stderr)
            return 2

    _print_decision(patient_id, result)
    flush()
    print("SMOKE OK — panel decision made before any clinical retrieval.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.openemr.panel",
        description="Live smoke: patient-panel gate before clinical retrieval.",
    )
    parser.add_argument("--provider", required=True, help="Provider (Practitioner) id, e.g. admin.")
    parser.add_argument("--patient", required=True, help="FHIR Patient id to gate.")
    parser.add_argument(
        "--break-glass",
        dest="reason",
        default=None,
        help="Break-glass reason; when given, forces an out-of-panel override.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_smoke(args.provider, args.patient, args.reason))


if __name__ == "__main__":
    raise SystemExit(main())
