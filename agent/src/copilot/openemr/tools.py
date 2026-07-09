"""Retrieval tools over OpenEMR FHIR (PRP M0-7 seed; grows in M1).

M0-7 ships exactly one tool — :func:`get_patient` — to prove the whole
foundation end-to-end: a user-bound, correlation-tagged, retrying FHIR read
whose raw resource is mapped into a typed :class:`~copilot.schemas.patient.Patient`
that carries its own grounding :class:`~copilot.schemas.core.SourceRef` (FR-8),
all wrapped in a Langfuse span (FR- observability).

The mapping layer is deliberately defensive: FHIR resources are loosely typed
and optional-field-heavy, so every access is guarded and a resource that cannot
satisfy the :class:`Patient` contract raises a typed :class:`~copilot.openemr.client.FhirError`
rather than producing a half-valid model.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from typing import Any

from copilot.config import Settings, get_settings
from copilot.logging import configure_logging, new_correlation_id, set_correlation_id
from copilot.observability import flush, trace
from copilot.openemr.client import FhirClient, FhirError
from copilot.openemr.oauth import OAuthError, TokenProvider, register_client
from copilot.schemas.core import SourceRef, ToolResult
from copilot.schemas.patient import Patient, Sex

__all__ = ["get_patient", "map_patient"]

_VALID_SEX: frozenset[str] = frozenset({"male", "female", "other", "unknown"})


# ---------------------------------------------------------------------------
# FHIR -> schema mapping
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse a FHIR ``instant``/``dateTime`` string into a ``datetime``.

    Tolerates a trailing ``Z`` (which ``datetime.fromisoformat`` rejects on some
    inputs) and returns ``None`` for anything unparseable rather than raising —
    a missing timestamp weakens grounding but must not fail the read.
    """

    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _display_name(resource: dict[str, Any]) -> str | None:
    """Best display name from a FHIR ``Patient.name`` array.

    Prefers a ``use == "official"`` entry, else the first; honours an explicit
    ``text``, otherwise joins ``given`` + ``family``.
    """

    names = resource.get("name")
    if not isinstance(names, list) or not names:
        return None

    chosen: dict[str, Any] | None = None
    for entry in names:
        if isinstance(entry, dict) and entry.get("use") == "official":
            chosen = entry
            break
    if chosen is None:
        chosen = next((n for n in names if isinstance(n, dict)), None)
    if chosen is None:
        return None

    text = chosen.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    given = chosen.get("given")
    given_str = " ".join(g for g in given if isinstance(g, str)) if isinstance(given, list) else ""
    family = chosen.get("family")
    family_str = family if isinstance(family, str) else ""
    full = f"{given_str} {family_str}".strip()
    return full or None


def _map_sex(value: Any) -> Sex:
    """Map FHIR ``administrativeGender`` to the ``Sex`` literal (fail-safe)."""

    if isinstance(value, str) and value.lower() in _VALID_SEX:
        return value.lower()  # type: ignore[return-value]
    return "unknown"


def _source_ref(resource: dict[str, Any], patient_id: str) -> SourceRef:
    """Build the grounding :class:`SourceRef` for a Patient resource."""

    meta = resource.get("meta")
    last_updated = meta.get("lastUpdated") if isinstance(meta, dict) else None
    return SourceRef(
        resource_type="Patient",
        id=patient_id,
        timestamp=_parse_ts(last_updated),
    )


def map_patient(resource: dict[str, Any]) -> Patient:
    """Map a raw FHIR ``Patient`` resource to the typed :class:`Patient`.

    Raises :class:`FhirError` if the resource is not a Patient or lacks the
    fields the :class:`Patient` contract requires (id, name, birthDate).
    """

    if resource.get("resourceType") != "Patient":
        raise FhirError(
            f"expected a FHIR Patient resource, got {resource.get('resourceType')!r}"
        )

    patient_id = resource.get("id")
    if not isinstance(patient_id, str) or not patient_id:
        raise FhirError("FHIR Patient resource is missing an 'id'")

    name = _display_name(resource)
    if name is None:
        raise FhirError(f"FHIR Patient {patient_id} has no usable name")

    birth = resource.get("birthDate")
    if not isinstance(birth, str) or not birth:
        raise FhirError(f"FHIR Patient {patient_id} is missing 'birthDate'")
    try:
        dob = date.fromisoformat(birth)
    except ValueError as exc:
        raise FhirError(
            f"FHIR Patient {patient_id} has an unparseable birthDate"
        ) from exc

    return Patient(
        id=patient_id,
        name=name,
        dob=dob,
        sex=_map_sex(resource.get("gender")),
        source=_source_ref(resource, patient_id),
    )


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


async def get_patient(
    patient_id: str,
    *,
    client: FhirClient,
) -> ToolResult[Patient]:
    """Read one patient by id and return a typed, grounded :class:`ToolResult`.

    ``GET {FHIR}/Patient/{id}`` → :class:`Patient`, with a :class:`SourceRef`
    grounding the result. The whole read is wrapped in a Langfuse span named
    ``get_patient`` (correlation-tagged, PHI-scrubbed); the raw FHIR body is
    never sent to the span — only the resource type/id and status.
    """

    with trace("get_patient", metadata={"resource_type": "Patient", "patient_id": patient_id}) as span:
        resource = await client.get(f"/Patient/{patient_id}")
        patient = map_patient(resource)
        result: ToolResult[Patient] = ToolResult(data=patient, sources=[patient.source])
        span.update(
            output={"resource_type": "Patient", "id": patient.id},
            metadata={"source_count": len(result.sources)},
        )
        return result


# ---------------------------------------------------------------------------
# CLI smoke — live end-to-end check against local OpenEMR (M0 acceptance)
# ---------------------------------------------------------------------------


async def _find_first_patient_id(client: FhirClient) -> str | None:
    """Return the id of the first patient in the FHIR store, if any."""

    bundle = await client.get("/Patient", params={"_count": 1})
    entries = bundle.get("entry")
    if isinstance(entries, list) and entries:
        resource = entries[0].get("resource") if isinstance(entries[0], dict) else None
        if isinstance(resource, dict):
            pid = resource.get("id")
            if isinstance(pid, str):
                return pid
    return None


async def _smoke(patient_id: str | None) -> int:
    """Fetch a real patient as the user token; print the typed result."""

    configure_logging()
    settings: Settings = get_settings()
    # A correlation ID for the whole smoke run — flows into agent logs, the
    # Langfuse span, and OpenEMR's api_log via X-Correlation-ID.
    correlation_id = new_correlation_id()
    set_correlation_id(correlation_id)
    print(f"FHIR smoke — {settings.openemr_fhir_base}")
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
            if patient_id is None:
                patient_id = await _find_first_patient_id(client)
                if patient_id is None:
                    print(
                        "BLOCKED: no patients found in local OpenEMR. Seed a "
                        "patient (UI: Patient/Client → New, or the REST API) "
                        "then re-run with --patient <id>.",
                        file=sys.stderr,
                    )
                    return 3
                print(f"  discovered patient id: {patient_id}")
            result = await get_patient(patient_id, client=client)
        except OAuthError as exc:
            print(f"BLOCKED: token acquisition failed: {exc}", file=sys.stderr)
            return 2
        except FhirError as exc:
            print(f"FAILED: FHIR read failed: {exc}", file=sys.stderr)
            return 2

    patient = result.data
    print("  Patient (typed):")
    print(f"    id:   {patient.id}")
    print(f"    name: {patient.name}")
    print(f"    dob:  {patient.dob.isoformat()}")
    print(f"    sex:  {patient.sex}")
    print("  SourceRef (grounding):")
    for src in result.sources:
        ts = src.timestamp.isoformat() if src.timestamp else "unknown"
        print(f"    {src.resource_type}/{src.id} @ {ts}")
    print("  retrieved_at:", result.retrieved_at.isoformat())
    flush()  # push the Langfuse span if observability is configured
    print("SMOKE OK — user-bound, typed, traced FHIR read.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.openemr.tools",
        description="Live smoke: authenticated, typed, traced FHIR patient read.",
    )
    parser.add_argument(
        "--patient",
        dest="patient",
        default=None,
        help="FHIR Patient id to read (omit to auto-discover the first patient).",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_smoke(args.patient))


if __name__ == "__main__":
    raise SystemExit(main())
