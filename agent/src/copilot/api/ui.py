"""Minimal demo UI + a patient-list endpoint that backs it.

``GET /`` serves a single self-contained page (``copilot/ui/index.html``) that
drives the streaming summary + conversation endpoints — a browser surface for the
grounded, cited output (for demos/review; the production UI is the SMART-embedded
panel). ``GET /patients`` lists a handful of patients (id + display name) as the
acting clinician (borrowed identity, FR-3) so the page's picker self-populates
without anyone typing a UUID.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from copilot.config import get_settings
from copilot.openemr.client import FhirClient
from copilot.openemr.oauth import TokenProvider, register_client
from copilot.openemr.tools import _display_name

__all__ = ["router", "get_ui_fhir_client"]

router = APIRouter(tags=["ui"])

_INDEX_HTML = Path(__file__).resolve().parent.parent / "ui" / "index.html"


async def get_ui_fhir_client() -> AsyncIterator[FhirClient]:
    """Yield a user-bound :class:`FhirClient` for one patient-list request.

    Same borrowed-identity wiring as the other endpoints (register client → dev
    user token → FHIR client); overridden in tests with a fake client.
    """

    settings = get_settings()
    creds = register_client(settings=settings)
    provider = TokenProvider(
        settings.openemr_dev_user,
        settings.openemr_dev_pass,
        settings=settings,
        credentials=creds,
    )
    async with FhirClient(provider, settings=settings) as client:
        yield client


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    """Serve the single-page demo UI.

    Sent ``no-store`` so the browser always fetches the current page instead of
    a stale cached copy — the UI is a single self-contained file that changes
    across builds, and a cached copy silently breaks click-to-source / layout.
    """

    return HTMLResponse(
        _INDEX_HTML.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/patients")
async def list_patients(
    limit: int = Query(default=25, ge=1, le=100),
    client: FhirClient = Depends(get_ui_fhir_client),
) -> list[dict[str, str]]:
    """List patients (``id`` + display name) as the acting clinician (FR-3).

    A thin ``GET /Patient?_count=`` read so the UI picker can populate. Returns
    only the id and a display name — no clinical values.
    """

    bundle = await client.get("/Patient", params={"_count": limit})
    patients: list[dict[str, str]] = []
    for entry in bundle.get("entry", []) or []:
        resource = entry.get("resource", {}) if isinstance(entry, dict) else {}
        patient_id = resource.get("id")
        if not isinstance(patient_id, str) or not patient_id:
            continue
        patients.append({"id": patient_id, "name": _display_name(resource) or "(unnamed)"})
    return patients
